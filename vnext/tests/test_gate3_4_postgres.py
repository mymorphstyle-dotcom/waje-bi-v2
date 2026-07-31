from __future__ import annotations

import os
import threading
import time
import unittest
from dataclasses import replace
from datetime import date
from uuid import uuid4

import psycopg

from gate1_fixtures import (
    NOW,
    accept_initial_question,
    make_frame,
    make_measurement_design,
    record_reviewed_frame,
)
from postgres_test_support import (
    bootstrap_postgres_test_schema,
    reset_postgres_test_data,
)
from test_gate3_3_measurement_resolver import (
    make_context,
    make_request,
    make_trusted_registry,
    make_trusted_resolver,
    make_trusted_verifier,
)
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    MailboxMessageKind,
    OperationIdentity,
)
from waje_vnext.domain.authority import CaseLifecycle
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.measurement import (
    MeasurementDerivationAuthority,
    ObligationExecutionDisposition,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationCompletion,
    ObligationCompletionRecord,
    ObligationDependency,
    ObligationDispatchRecord,
    ObligationPlanBinding,
    ObligationScheduleRecord,
    ObligationTerminalStatus,
    build_obligation_schedule_id,
    build_obligation_dispatch,
)
from waje_vnext.domain.planning import (
    PlanBundle,
    ProposedWorkTask,
    build_conformance_execution_spec,
    build_logical_execution_attempt,
    compile_plan_bundle,
)
from waje_vnext.domain.runtime_state import OutboxMessage
from waje_vnext.storage.ports import (
    AuthorityConflict,
    AuthorityNotFound,
    InvalidAuthorityTransition,
    StaleHead,
)
from waje_vnext.storage.postgres import (
    PostgresAuthorityStore,
)


DSN = os.environ.get("WAJE_VNEXT_DATABASE_URL")


@unittest.skipUnless(
    DSN,
    "WAJE_VNEXT_DATABASE_URL is not configured",
)
class Gate34PostgresStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        bootstrap_postgres_test_schema(DSN)

    def setUp(self) -> None:
        assert DSN is not None
        reset_postgres_test_data(DSN)
        self.case_id = f"g34-{uuid4().hex}"
        self.store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_reset_requires_database_owned_disposable_token(self) -> None:
        valid_token = os.environ[
            "WAJE_VNEXT_TEST_DATABASE_RESET_TOKEN"
        ]
        os.environ["WAJE_VNEXT_TEST_DATABASE_RESET_TOKEN"] = (
            "wrong-database-token"
        )
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "reset token is absent",
            ):
                assert DSN is not None
                reset_postgres_test_data(DSN)
        finally:
            os.environ[
                "WAJE_VNEXT_TEST_DATABASE_RESET_TOKEN"
            ] = valid_token

    def test_plan_bundle_and_logical_retry_round_trip(self) -> None:
        bundle = self._build_plan_bundle()
        accepted = self.store.accept_plan_bundle(
            bundle,
            expected_head_version=bundle.adoption.expected_head_version,
            event_id=f"{self.case_id}:event:plan",
            recorded_at=NOW,
        )

        self.assertEqual(
            accepted.accepted_plan_revision_id,
            bundle.plan.plan_revision_id,
        )
        self.assertEqual(
            self.store.get_plan_adoption(
                bundle.plan.plan_revision_id
            ),
            bundle.adoption,
        )
        self.assertEqual(
            self.store.list_query_bindings(
                bundle.plan.plan_revision_id
            ),
            bundle.query_bindings,
        )
        derived_events = {
            event.event_type: event
            for event in self.store.list_events(self.case_id)
            if event.event_type
            in {
                JournalEventType.MEASUREMENT_RESOLUTION_RECORDED,
                JournalEventType.EVIDENCE_OBLIGATION_RECORDED,
            }
        }
        resolution_event = derived_events[
            JournalEventType.MEASUREMENT_RESOLUTION_RECORDED
        ]
        obligation_event = derived_events[
            JournalEventType.EVIDENCE_OBLIGATION_RECORDED
        ]
        self.assertEqual(
            resolution_event.operation.causation_id,
            self.resolution_operation.operation_id,
        )
        self.assertEqual(
            obligation_event.operation.causation_id,
            self.obligation_operations[0].operation_id,
        )
        self.assertEqual(
            {
                resolution_event.operation.correlation_id,
                obligation_event.operation.correlation_id,
            },
            {f"{self.case_id}:run:1"},
        )

        binding = bundle.query_bindings[0]
        spec = build_conformance_execution_spec(
            query_binding=binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "payment-contrast.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "payment-contrast.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "payment-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/"
                "conformance.v1"
            ),
            created_at=NOW,
        )
        accepted_snapshot = self.store.get_authority_snapshot(
            self.case_id
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "not system-derived",
        ):
            self.store.record_conformance_execution_spec(
                replace(
                    spec,
                    logical_execution_id="a" * 64,
                    conformance_execution_spec_id="b" * 64,
                ),
                expected_authority_snapshot=accepted_snapshot,
            )
        self.assertEqual(
            self.store.record_conformance_execution_spec(
                spec,
                expected_authority_snapshot=accepted_snapshot,
            ),
            spec,
        )
        second_valid_spec = build_conformance_execution_spec(
            query_binding=binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "second-payment-contrast.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "second-payment-contrast.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "payment-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/"
                "conformance.v1"
            ),
            created_at=NOW,
        )
        with self.assertRaises(AuthorityConflict):
            self.store.record_conformance_execution_spec(
                second_valid_spec,
                expected_authority_snapshot=accepted_snapshot,
            )
        self.assertEqual(
            self.store.get_conformance_execution_spec(
                spec.conformance_execution_spec_id
            ),
            spec,
        )

        initial = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=accepted_snapshot,
            attempt_number=1,
            prior_attempt=None,
            retry_reason_code=None,
            requested_at=NOW,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "attempt is not system-derived",
        ):
            self.store.record_logical_execution_attempt(
                replace(
                    initial,
                    logical_execution_attempt_id="c" * 64,
                )
            )
        self.assertEqual(
            self.store.record_logical_execution_attempt(initial),
            initial,
        )
        self.assertEqual(
            self.store.record_logical_execution_attempt(initial),
            initial,
        )
        retry = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=accepted_snapshot,
            attempt_number=2,
            prior_attempt=initial,
            retry_reason_code="provider_timeout",
            requested_at=NOW,
        )
        def rederive_id(attempt):
            return replace(
                attempt,
                logical_execution_attempt_id=content_sha256(
                    {
                        "kind": "logical-execution-attempt.v1",
                        "logical_execution_id": (
                            attempt.logical_execution_id
                        ),
                        "attempt_number": attempt.attempt_number,
                        "prior_attempt_id": attempt.prior_attempt_id,
                        "retry_reason_code": (
                            attempt.retry_reason_code
                        ),
                    }
                ),
            )

        forged_retries = {
            "prior_attempt_id": rederive_id(
                replace(retry, prior_attempt_id="e" * 64)
            ),
            "task_id": rederive_id(
                replace(retry, task_id=f"{self.case_id}:forged-task")
            ),
            "query_binding_content_sha256": rederive_id(
                replace(
                    retry,
                    query_binding_content_sha256="f" * 64,
                )
            ),
            "execution_spec_content_sha256": rederive_id(
                replace(
                    retry,
                    execution_spec_content_sha256="a" * 64,
                )
            ),
        }
        for changed_field, forged_retry in forged_retries.items():
            with self.subTest(changed_field=changed_field):
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "sealed.*input",
                ):
                    self.store.record_logical_execution_attempt(
                        forged_retry
                    )
                self.assertEqual(
                    self.store.list_logical_execution_attempts(
                        spec.logical_execution_id
                    ),
                    (initial,),
                )
        self.store.record_logical_execution_attempt(retry)
        self.assertEqual(
            self.store.list_logical_execution_attempts(
                spec.logical_execution_id
            ),
            (initial, retry),
        )

    def test_tampered_query_binding_aborts_entire_plan_adoption(
        self,
    ) -> None:
        bundle = self._build_plan_bundle()
        forged_binding = replace(
            bundle.query_bindings[0],
            capability_intent_ref=(
                "waje-vnext://capability-intent/"
                "unrelated-analysis.v1"
            ),
        )
        forged = PlanBundle(
            plan=bundle.plan,
            query_bindings=(forged_binding,),
            adoption=bundle.adoption,
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "query binding changes measurement authority",
        ):
            self.store.accept_plan_bundle(
                forged,
                expected_head_version=(
                    bundle.adoption.expected_head_version
                ),
                event_id=f"{self.case_id}:event:forged-plan",
                recorded_at=NOW,
            )

        with self.assertRaises(AuthorityNotFound):
            self.store.get_plan(bundle.plan.plan_revision_id)

    def test_multiple_evidence_slots_persist_as_distinct_obligations(
        self,
    ) -> None:
        bundle = self._build_plan_bundle(
            evidence_type_refs=(
                "evidence:primary-estimate",
                "evidence:independent-reconciliation",
            )
        )
        obligations = tuple(
            self.store.get_evidence_obligation(obligation_id)
            for obligation_id in bundle.adoption.obligation_ids
        )
        self.assertEqual(len(obligations), 2)
        self.assertEqual(
            {item.evidence_type_refs for item in obligations},
            {
                ("evidence:primary-estimate",),
                ("evidence:independent-reconciliation",),
            },
        )
        self.assertEqual(
            len({item.content_sha256 for item in obligations}),
            2,
        )
        accepted = self.store.accept_plan_bundle(
            bundle,
            expected_head_version=(
                bundle.adoption.expected_head_version
            ),
            event_id=f"{self.case_id}:event:plan:multi-slot",
            recorded_at=NOW,
        )
        self.assertEqual(
            accepted.accepted_plan_revision_id,
            bundle.plan.plan_revision_id,
        )

    def test_boundary_and_mixed_plan_shapes_round_trip(self) -> None:
        boundary_bundle = self._build_plan_bundle(boundary=True)
        self.assertEqual(boundary_bundle.query_bindings, ())
        accepted = self.store.accept_plan_bundle(
            boundary_bundle,
            expected_head_version=(
                boundary_bundle.adoption.expected_head_version
            ),
            event_id=f"{self.case_id}:event:plan:boundary",
            recorded_at=NOW,
        )
        self.assertEqual(
            accepted.accepted_plan_revision_id,
            boundary_bundle.plan.plan_revision_id,
        )
        self.assertEqual(
            self.store.list_query_bindings(
                boundary_bundle.plan.plan_revision_id
            ),
            (),
        )

        self.case_id = f"g34-mixed-{uuid4().hex}"
        mixed_bundle = self._build_plan_bundle(mixed=True)
        obligations = tuple(
            self.store.get_evidence_obligation(obligation_id)
            for obligation_id in mixed_bundle.adoption.obligation_ids
        )
        self.assertEqual(
            {item.execution_disposition for item in obligations},
            {
                ObligationExecutionDisposition.EXECUTABLE,
                ObligationExecutionDisposition.TYPED_BOUNDARY,
            },
        )
        self.assertEqual(len(mixed_bundle.query_bindings), 1)
        accepted = self.store.accept_plan_bundle(
            mixed_bundle,
            expected_head_version=(
                mixed_bundle.adoption.expected_head_version
            ),
            event_id=f"{self.case_id}:event:plan:mixed",
            recorded_at=NOW,
        )
        self.assertEqual(
            accepted.accepted_plan_revision_id,
            mixed_bundle.plan.plan_revision_id,
        )
        schedule = self._build_schedule(mixed_bundle)
        self.assertEqual(
            self.store.record_obligation_schedule(schedule),
            schedule,
        )
        boundary = next(
            item
            for item in obligations
            if item.execution_disposition
            is ObligationExecutionDisposition.TYPED_BOUNDARY
        )
        boundary_binding = next(
            item
            for item in schedule.plan_bindings
            if item.obligation_id == boundary.obligation_id
        )
        self.assertIsNone(boundary_binding.query_binding_id)
        with self.assertRaisesRegex(ValueError, "query binding"):
            build_obligation_dispatch(
                obligation=boundary,
                plan_binding=boundary_binding,
                plan_revision_id=schedule.plan_revision_id,
                current_authority=schedule.authority_snapshot,
            )

    def test_plan_adoption_is_idempotent_and_payload_conflicts(self) -> None:
        bundle = self._build_plan_bundle()
        event_id = f"{self.case_id}:event:plan:idempotent"
        first = self.store.accept_plan_bundle(
            bundle,
            expected_head_version=bundle.adoption.expected_head_version,
            event_id=event_id,
            recorded_at=NOW,
        )
        second = self.store.accept_plan_bundle(
            bundle,
            expected_head_version=bundle.adoption.expected_head_version,
            event_id=event_id,
            recorded_at=NOW,
        )
        self.assertEqual(first, second)
        forged = replace(
            bundle,
            plan=replace(
                bundle.plan,
                revision_reason="Different payload under the same identity",
            ),
        )
        with self.assertRaises(AuthorityConflict):
            self.store.accept_plan_bundle(
                forged,
                expected_head_version=(
                    bundle.adoption.expected_head_version
                ),
                event_id=event_id,
                recorded_at=NOW,
            )

        self.case_id = f"g34-operation-replay-{uuid4().hex}"
        operation_bundle = self._build_plan_bundle()
        operation = OperationIdentity(
            operation_id=f"{self.case_id}:operation:plan",
            idempotency_key=f"{self.case_id}:operation:plan:key",
            causation_id=f"{self.case_id}:user-turn",
            correlation_id=self.case_id,
            authority_revision=(
                self.store.get_mailbox_head(
                    self.case_id
                ).authority_epoch
            ),
            payload_sha256=content_sha256(
                {"kind": "plan-operation-replay"}
            ),
        )
        operation_event_id = f"{self.case_id}:event:plan"
        self.store.accept_plan_bundle(
            operation_bundle,
            expected_head_version=(
                operation_bundle.adoption.expected_head_version
            ),
            event_id=operation_event_id,
            recorded_at=NOW,
            operation=operation,
        )
        with self.assertRaises(AuthorityConflict):
            self.store.accept_plan_bundle(
                operation_bundle,
                expected_head_version=(
                    operation_bundle.adoption.expected_head_version
                ),
                event_id=operation_event_id,
                recorded_at=NOW,
                operation=None,
            )
        changed_operations = {
            "idempotency_key": replace(
                operation,
                idempotency_key=f"{self.case_id}:changed-key",
            ),
            "causation_id": replace(
                operation,
                causation_id=f"{self.case_id}:changed-causation",
            ),
            "payload_sha256": replace(
                operation,
                payload_sha256=content_sha256(
                    {"kind": "changed-plan-operation"}
                ),
            ),
        }
        for changed_field, changed_operation in (
            changed_operations.items()
        ):
            with self.subTest(changed_field=changed_field):
                with self.assertRaises(AuthorityConflict):
                    self.store.accept_plan_bundle(
                        operation_bundle,
                        expected_head_version=(
                            operation_bundle.adoption
                            .expected_head_version
                        ),
                        event_id=operation_event_id,
                        recorded_at=NOW,
                        operation=changed_operation,
                    )

    def test_concurrent_plans_use_one_head_compare_and_swap(self) -> None:
        first_bundle = self._build_plan_bundle()
        second_bundle = self._compile_competing_bundle(
            plan_revision_id=f"{self.case_id}:plan:competing"
        )
        stores = (
            PostgresAuthorityStore.connect(
                DSN,
                resolution_input_verifier=make_trusted_verifier(),
            ),
            PostgresAuthorityStore.connect(
                DSN,
                resolution_input_verifier=make_trusted_verifier(),
            ),
        )
        barrier = threading.Barrier(2)
        results: list[object] = []

        def accept(index: int, bundle: PlanBundle) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    stores[index].accept_plan_bundle(
                        bundle,
                        expected_head_version=(
                            bundle.adoption.expected_head_version
                        ),
                        event_id=(
                            f"{self.case_id}:event:plan:race:{index}"
                        ),
                        recorded_at=NOW,
                    )
                )
            except Exception as error:
                results.append(error)

        threads = (
            threading.Thread(target=accept, args=(0, first_bundle)),
            threading.Thread(target=accept, args=(1, second_bundle)),
        )
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        finally:
            for store in stores:
                store.close()
        self.assertEqual(
            sum(not isinstance(item, Exception) for item in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(item, StaleHead) for item in results),
            1,
        )
        persisted = tuple(
            plan_id
            for plan_id in (
                first_bundle.plan.plan_revision_id,
                second_bundle.plan.plan_revision_id,
            )
            if self._plan_exists(plan_id)
        )
        self.assertEqual(len(persisted), 1)

    def test_plan_adoption_rolls_back_after_mid_transaction_failure(
        self,
    ) -> None:
        bundle = self._build_plan_bundle()
        assert DSN is not None
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(
                """
                CREATE FUNCTION public.fail_g34_plan_adoption()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    RAISE EXCEPTION 'injected plan adoption failure';
                END;
                $$
                """
            )
            connection.execute(
                """
                CREATE TRIGGER fail_g34_plan_adoption
                BEFORE INSERT ON waje_vnext.plan_adoption_records
                FOR EACH ROW
                EXECUTE FUNCTION public.fail_g34_plan_adoption()
                """
            )
        try:
            with self.assertRaisesRegex(
                psycopg.errors.RaiseException,
                "injected plan adoption failure",
            ):
                self.store.accept_plan_bundle(
                    bundle,
                    expected_head_version=(
                        bundle.adoption.expected_head_version
                    ),
                    event_id=f"{self.case_id}:event:plan:injected-failure",
                    recorded_at=NOW,
                )
        finally:
            with psycopg.connect(DSN, autocommit=True) as connection:
                connection.execute(
                    """
                    DROP TRIGGER IF EXISTS fail_g34_plan_adoption
                    ON waje_vnext.plan_adoption_records
                    """
                )
                connection.execute(
                    """
                    DROP FUNCTION IF EXISTS public.fail_g34_plan_adoption()
                    """
                )
        with self.assertRaises(AuthorityNotFound):
            self.store.get_plan(bundle.plan.plan_revision_id)
        self.assertIsNone(
            self.store.get_case(self.case_id).accepted_plan_revision_id
        )
        self.assertNotIn(
            JournalEventType.PLAN_ACCEPTED,
            {
                event.event_type
                for event in self.store.list_events(self.case_id)
            },
        )

    def test_user_correction_fences_stale_logical_attempt(self) -> None:
        bundle = self._build_plan_bundle()
        self.store.accept_plan_bundle(
            bundle,
            expected_head_version=bundle.adoption.expected_head_version,
            event_id=f"{self.case_id}:event:plan",
            recorded_at=NOW,
        )
        snapshot = self.store.get_authority_snapshot(self.case_id)
        spec = build_conformance_execution_spec(
            query_binding=bundle.query_bindings[0],
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "payment-contrast.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "payment-contrast.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "payment-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/"
                "conformance.v1"
            ),
            created_at=NOW,
        )
        self.store.record_conformance_execution_spec(
            spec,
            expected_authority_snapshot=snapshot,
        )
        attempt = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=snapshot,
            attempt_number=1,
            prior_attempt=None,
            retry_reason_code=None,
            requested_at=NOW,
        )

        payload = {"message": "请改用最近完整结算日"}
        self.store.append_mailbox_message(
            message_id=f"{self.case_id}:message:correction",
            case_id=self.case_id,
            kind=MailboxMessageKind.USER_MESSAGE,
            operation=OperationIdentity(
                operation_id=f"{self.case_id}:operation:correction",
                idempotency_key=(
                    f"{self.case_id}:correction-message-key"
                ),
                causation_id=f"{self.case_id}:user-turn:2",
                correlation_id=self.case_id,
                authority_revision=(
                    snapshot.mailbox_authority_epoch
                ),
                payload_sha256=content_sha256(payload),
            ),
            payload=payload,
            created_at=NOW,
        )

        self.assertEqual(
            self.store.record_conformance_execution_spec(
                spec,
                expected_authority_snapshot=snapshot,
            ),
            spec,
        )
        with self.assertRaisesRegex(
            StaleHead,
            "logical execution attempt authority is stale",
        ):
            self.store.record_logical_execution_attempt(attempt)
        self.assertEqual(
            self.store.list_logical_execution_attempts(
                spec.logical_execution_id
            ),
            (),
        )

    def test_user_correction_fences_stale_plan_adoption(self) -> None:
        bundle = self._build_plan_bundle()
        snapshot = bundle.adoption.authority_snapshot
        payload = {"message": "请改用新的支付口径"}
        self.store.append_mailbox_message(
            message_id=f"{self.case_id}:message:correction",
            case_id=self.case_id,
            kind=MailboxMessageKind.USER_MESSAGE,
            operation=OperationIdentity(
                operation_id=f"{self.case_id}:operation:correction",
                idempotency_key=(
                    f"{self.case_id}:correction-message-key"
                ),
                causation_id=f"{self.case_id}:user-turn:2",
                correlation_id=self.case_id,
                authority_revision=(
                    snapshot.mailbox_authority_epoch
                ),
                payload_sha256=content_sha256(payload),
            ),
            payload=payload,
            created_at=NOW,
        )

        with self.assertRaisesRegex(
            StaleHead,
            "plan adoption authority snapshot is stale",
        ):
            self.store.accept_plan_bundle(
                bundle,
                expected_head_version=(
                    bundle.adoption.expected_head_version
                ),
                event_id=f"{self.case_id}:event:stale-plan",
                recorded_at=NOW,
            )
        self.assertIsNone(
            self.store.get_case(
                self.case_id
            ).accepted_plan_revision_id
        )
        with self.assertRaises(AuthorityNotFound):
            self.store.get_plan(bundle.plan.plan_revision_id)

    def test_schedule_and_dispatch_replay_accepted_plan_bindings(
        self,
    ) -> None:
        bundle = self._build_plan_bundle()
        self.store.accept_plan_bundle(
            bundle,
            expected_head_version=bundle.adoption.expected_head_version,
            event_id=f"{self.case_id}:event:plan",
            recorded_at=NOW,
        )
        schedule = self._build_schedule(bundle)
        self.assertEqual(
            self.store.record_obligation_schedule(schedule),
            schedule,
        )
        self.assertEqual(
            self.store.get_obligation_schedule(schedule.schedule_id),
            schedule,
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "schedule ID is not canonical",
        ):
            self.store.record_obligation_schedule(
                replace(schedule, schedule_id="forged-schedule-id")
            )

        forged_schedule = replace(
            schedule,
            plan_bindings=(
                replace(
                    schedule.plan_bindings[0],
                    query_binding_id="f" * 64,
                ),
            ),
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "schedule task/query bindings were not Plan-derived",
        ):
            self.store.record_obligation_schedule(
                forged_schedule
            )

        obligation = schedule.obligations[0]
        plan_binding = schedule.plan_bindings[0]
        dispatch = build_obligation_dispatch(
            obligation=obligation,
            plan_binding=plan_binding,
            plan_revision_id=schedule.plan_revision_id,
            current_authority=schedule.authority_snapshot,
        )
        correct_outbox = self._build_dispatch_outbox(
            schedule=schedule,
            dispatch=dispatch,
            obligation=obligation,
            suffix="correct",
        )
        dispatch_record = ObligationDispatchRecord(
            dispatch_record_id=content_sha256(
                {
                    "dispatch": dispatch.dispatch_id,
                    "outbox": correct_outbox.outbox_message_id,
                }
            ),
            schedule_id=schedule.schedule_id,
            outbox_message_id=correct_outbox.outbox_message_id,
            dispatch=dispatch,
            created_at=NOW,
        )
        self.assertEqual(
            self.store.record_obligation_dispatch(
                message=correct_outbox,
                record=dispatch_record,
            ),
            dispatch_record,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "requires schedule dispatch admission",
        ):
            self.store.enqueue_outbox(correct_outbox)

        for field_name, forged_value in (
            ("plan_revision_id", f"{self.case_id}:plan:forged"),
            ("task_id", "d" * 64),
            ("query_binding_id", "e" * 64),
            (
                "obligation",
                {"forged": "different persisted obligation"},
            ),
        ):
            with self.subTest(outbox_field=field_name):
                mismatched_outbox = self._build_dispatch_outbox(
                    schedule=schedule,
                    dispatch=dispatch,
                    obligation=obligation,
                    suffix=f"mismatched-{field_name}",
                    payload_overrides={
                        field_name: forged_value,
                    },
                )
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "does not exactly bind",
                ):
                    self.store.record_obligation_dispatch(
                        message=mismatched_outbox,
                        record=replace(
                            dispatch_record,
                            dispatch_record_id=content_sha256(
                                {
                                    "dispatch": (
                                        dispatch.dispatch_id
                                    ),
                                    "outbox": (
                                        mismatched_outbox
                                        .outbox_message_id
                                    ),
                                }
                            ),
                            outbox_message_id=(
                                mismatched_outbox.outbox_message_id
                            ),
                        )
                    )
        correlation_outbox = self._build_dispatch_outbox(
            schedule=schedule,
            dispatch=dispatch,
            obligation=obligation,
            suffix="mismatched-correlation",
        )
        correlation_outbox = replace(
            correlation_outbox,
            operation=replace(
                correlation_outbox.operation,
                correlation_id=f"{self.case_id}:run:forged",
            ),
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "does not exactly bind",
        ):
            self.store.record_obligation_dispatch(
                message=correlation_outbox,
                record=replace(
                    dispatch_record,
                    dispatch_record_id=content_sha256(
                        {
                            "dispatch": dispatch.dispatch_id,
                            "outbox": (
                                correlation_outbox.outbox_message_id
                            ),
                        }
                    ),
                    outbox_message_id=(
                        correlation_outbox.outbox_message_id
                    ),
                ),
            )

    def test_terminal_lifecycle_fences_persisted_obligation_work(
        self,
    ) -> None:
        base_case_id = self.case_id
        for lifecycle in (
            CaseLifecycle.STOPPED,
            CaseLifecycle.CLOSED,
        ):
            with self.subTest(lifecycle=lifecycle.value):
                self.case_id = (
                    f"{base_case_id}-{lifecycle.value}"
                )
                bundle = self._build_plan_bundle()
                self.store.accept_plan_bundle(
                    bundle,
                    expected_head_version=(
                        bundle.adoption.expected_head_version
                    ),
                    event_id=f"{self.case_id}:event:plan",
                    recorded_at=NOW,
                )
                schedule = self._build_schedule(bundle)
                self.store.record_obligation_schedule(schedule)
                obligation = schedule.obligations[0]
                dispatch = build_obligation_dispatch(
                    obligation=obligation,
                    plan_binding=schedule.plan_bindings[0],
                    plan_revision_id=schedule.plan_revision_id,
                    current_authority=schedule.authority_snapshot,
                )
                message = self._build_dispatch_outbox(
                    schedule=schedule,
                    dispatch=dispatch,
                    obligation=obligation,
                    suffix="before-terminal",
                )
                dispatch_record = ObligationDispatchRecord(
                    dispatch_record_id=content_sha256(
                        {
                            "dispatch": dispatch.dispatch_id,
                            "outbox": message.outbox_message_id,
                        }
                    ),
                    schedule_id=schedule.schedule_id,
                    outbox_message_id=message.outbox_message_id,
                    dispatch=dispatch,
                    created_at=NOW,
                )
                self.store.record_obligation_dispatch(
                    message=message,
                    record=dispatch_record,
                )
                case = self.store.get_case(self.case_id)
                terminal = self.store.transition_case_lifecycle(
                    case_id=self.case_id,
                    lifecycle=lifecycle,
                    expected_head_version=case.head_version,
                    event_id=(
                        f"{self.case_id}:event:{lifecycle.value}"
                    ),
                    action_id=(
                        f"{self.case_id}:action:{lifecycle.value}"
                    ),
                    recorded_at=NOW,
                )
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "terminal case fences obligation dispatch",
                ):
                    self.store.record_obligation_dispatch(
                        message=message,
                        record=dispatch_record,
                    )
                completion = ObligationCompletionRecord(
                    completion_record_id=content_sha256(
                        {
                            "completion": obligation.obligation_id,
                            "lifecycle": lifecycle.value,
                        }
                    ),
                    schedule_id=schedule.schedule_id,
                    completion=ObligationCompletion(
                        obligation_id=obligation.obligation_id,
                        dispatch_id=dispatch.dispatch_id,
                        status=(
                            ObligationTerminalStatus.EXECUTION_SUCCEEDED
                        ),
                        result_sha256=content_sha256(
                            {"result": "arrived-after-terminal"}
                        ),
                    ),
                    admitted_authority_snapshot_sha256=(
                        self.store.get_authority_snapshot(
                            self.case_id
                        ).content_sha256
                    ),
                    created_at=NOW,
                )
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "terminal case fences obligation completion",
                ):
                    self.store.record_obligation_completion(
                        completion
                    )
                self.assertEqual(
                    self.store.list_obligation_dispatches(
                        schedule.schedule_id
                    ),
                    (dispatch_record,),
                )
                self.assertEqual(
                    self.store.list_obligation_completions(
                        schedule.schedule_id
                    ),
                    (),
                )
                self.assertEqual(terminal.lifecycle, lifecycle)
        self.case_id = base_case_id

    def _build_schedule(
        self,
        bundle: PlanBundle,
    ) -> ObligationScheduleRecord:
        authority = self.store.get_authority_snapshot(self.case_id)
        obligations = tuple(
            self.store.get_evidence_obligation(obligation_id)
            for obligation_id in bundle.adoption.obligation_ids
        )
        task_by_obligation = {
            obligation_id: task
            for task in bundle.plan.tasks
            for obligation_id in task.obligation_ids
        }
        query_by_obligation = {
            item.obligation_id: item
            for item in bundle.query_bindings
        }
        plan_bindings = tuple(
            ObligationPlanBinding(
                obligation_id=obligation.obligation_id,
                task_id=task_by_obligation[
                    obligation.obligation_id
                ].task_id,
                query_binding_id=(
                    query_by_obligation[
                        obligation.obligation_id
                    ].query_binding_id
                    if obligation.execution_disposition
                    is ObligationExecutionDisposition.EXECUTABLE
                    else None
                ),
            )
            for obligation in obligations
        )
        correlation_id = f"{self.case_id}:run:1"
        return ObligationScheduleRecord(
            schedule_id=build_obligation_schedule_id(
                case_id=self.case_id,
                correlation_id=correlation_id,
                frame_revision_id=bundle.plan.frame_revision_id,
                plan_revision_id=bundle.plan.plan_revision_id,
                plan_adoption_id=bundle.adoption.plan_adoption_id,
                plan_adoption_content_sha256=(
                    bundle.adoption.content_sha256
                ),
                authority=authority,
            ),
            case_id=self.case_id,
            correlation_id=correlation_id,
            frame_revision_id=bundle.plan.frame_revision_id,
            plan_revision_id=bundle.plan.plan_revision_id,
            plan_adoption_id=bundle.adoption.plan_adoption_id,
            plan_adoption_content_sha256=(
                bundle.adoption.content_sha256
            ),
            obligations=obligations,
            plan_bindings=plan_bindings,
            dependencies=tuple(
                ObligationDependency(
                    obligation_id=obligation.obligation_id,
                    depends_on_obligation_ids=(),
                )
                for obligation in obligations
            ),
            authority_snapshot=authority,
            authority_snapshot_sha256=authority.content_sha256,
            created_at=NOW,
        )

    def _build_dispatch_outbox(
        self,
        *,
        schedule: ObligationScheduleRecord,
        dispatch,
        obligation,
        suffix: str,
        payload_overrides: dict[str, object] | None = None,
    ) -> OutboxMessage:
        payload = {
            "schedule_id": schedule.schedule_id,
            "obligation_id": obligation.obligation_id,
            "plan_revision_id": dispatch.plan_revision_id,
            "task_id": dispatch.task_id,
            "query_binding_id": dispatch.query_binding_id,
            "obligation": to_jsonable(obligation),
            "dispatch_id": dispatch.dispatch_id,
        }
        payload.update(payload_overrides or {})
        outbox_message_id = (
            f"{self.case_id}:outbox:dispatch:{suffix}"
        )
        dispatch_record_id = content_sha256(
            {
                "dispatch": dispatch.dispatch_id,
                "outbox": outbox_message_id,
            }
        )
        event_payload = {
            key: value
            for key, value in payload.items()
            if key != "obligation"
        }
        event_payload["outbox_message_id"] = outbox_message_id
        event_operation = OperationIdentity(
            operation_id=(
                f"{self.case_id}:operation:dispatch-event:{suffix}"
            ),
            idempotency_key=(
                f"{self.case_id}:dispatch-event-key:{suffix}"
            ),
            causation_id=schedule.schedule_id,
            correlation_id=schedule.correlation_id,
            authority_revision=(
                schedule.authority_snapshot.mailbox_authority_epoch
            ),
            payload_sha256=content_sha256(event_payload),
        )
        event = self.store.append_event(
            case_id=self.case_id,
            expected_next_cursor=(
                len(self.store.list_events(self.case_id)) + 1
            ),
            event_id=f"{self.case_id}:event:dispatch:{suffix}",
            event_type=(
                JournalEventType.OBLIGATION_DISPATCH_ENQUEUED
            ),
            recorded_at=NOW,
            action_id=None,
            authority_ref=dispatch_record_id,
            payload=event_payload,
            customer_projection=None,
            operation=event_operation,
        )
        operation = OperationIdentity(
            operation_id=(
                f"{self.case_id}:operation:outbox:{suffix}"
            ),
            idempotency_key=(
                f"{self.case_id}:outbox-key:{suffix}"
            ),
            causation_id=event.operation.operation_id,
            correlation_id=schedule.correlation_id,
            authority_revision=(
                schedule.authority_snapshot.mailbox_authority_epoch
            ),
            payload_sha256=content_sha256(payload),
        )
        message = OutboxMessage(
            outbox_message_id=outbox_message_id,
            case_id=self.case_id,
            source_event_cursor=event.cursor,
            action_id=None,
            job_kind=AsyncJobKind.OBLIGATION,
            operation=operation,
            expected_head_version=(
                schedule.authority_snapshot.head_version
            ),
            expected_authority_epoch=(
                schedule.authority_snapshot.mailbox_authority_epoch
            ),
            authority_snapshot=schedule.authority_snapshot,
            authority_snapshot_sha256=(
                schedule.authority_snapshot_sha256
            ),
            idempotency_key=operation.idempotency_key,
            destination="obligation-worker",
            contract_ref=(
                "waje-vnext://runtime/"
                "resolved-evidence-obligation-job.v1"
            ),
            payload=payload,
            payload_sha256=content_sha256(payload),
            created_at=NOW,
        )
        return message

    def _build_plan_bundle(
        self,
        *,
        evidence_type_refs: tuple[str, ...] | None = None,
        boundary: bool = False,
        mixed: bool = False,
    ) -> PlanBundle:
        case = self.store.open_case(
            case_id=self.case_id,
            thread_id=f"{self.case_id}:thread",
            event_id=f"{self.case_id}:event:open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(self.store, case)
        design = None
        if evidence_type_refs is not None:
            design = make_measurement_design(
                question_id=question.question_revision_id,
                source_message_id=question.source_messages[0].message_id,
                source_text=question.source_messages[0].content,
            )
            design = replace(
                design,
                evidence_requirements=(
                    replace(
                        design.evidence_requirements[0],
                        required_evidence_type_refs=evidence_type_refs,
                    ),
                ),
            )
        if mixed:
            design = design or make_measurement_design(
                question_id=question.question_revision_id,
                source_message_id=question.source_messages[0].message_id,
                source_text=question.source_messages[0].content,
            )
            first_estimand = design.estimands[0]
            first_requirement = design.evidence_requirements[0]
            second_estimand_id = f"{self.case_id}:estimand:boundary"
            second_scope = replace(
                design.scopes[0],
                scope_id=f"{self.case_id}:scope:boundary",
                predicate_ref="predicate:mixed-boundary-population",
            )
            second_requirement = replace(
                first_requirement,
                evidence_requirement_id=(
                    f"{self.case_id}:requirement:boundary"
                ),
                target_estimand_ids=(second_estimand_id,),
                scope_id=second_scope.scope_id,
            )
            second_estimand = replace(
                first_estimand,
                estimand_id=second_estimand_id,
                evidence_requirement_ids=(
                    second_requirement.evidence_requirement_id,
                ),
                scope_ceiling_id=second_scope.scope_id,
            )
            second_completion = replace(
                design.completion_specs[0],
                completion_spec_id=(
                    f"{self.case_id}:completion:boundary"
                ),
                target_estimand_ids=(second_estimand_id,),
                required_evidence_requirement_ids=(
                    second_requirement.evidence_requirement_id,
                ),
            )
            design = replace(
                design,
                evidence_requirements=(
                    first_requirement,
                    second_requirement,
                ),
                completion_specs=(
                    design.completion_specs[0],
                    second_completion,
                ),
                scopes=(design.scopes[0], second_scope),
                estimands=(first_estimand, second_estimand),
            )
        frame = make_frame(
            case_id=self.case_id,
            question=question,
            frame_id=f"{self.case_id}:frame:1",
            measurement_design=design,
        )
        proof_id = record_reviewed_frame(self.store, frame)
        case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id=f"{self.case_id}:event:frame",
            recorded_at=NOW,
        )
        self.frame = frame
        requests_by_estimand_id = None
        if boundary:
            requests_by_estimand_id = {
                frame.measurement_design.estimands[0].estimand_id: (
                    make_request(
                        frame,
                        anchor=date(2026, 7, 1),
                        expected="7",
                        observed="6",
                        valid="6",
                        invalid="0",
                        missing="1",
                    )
                )
            }
        elif mixed:
            first, second = frame.measurement_design.estimands
            requests_by_estimand_id = {
                first.estimand_id: make_request(
                    frame,
                    anchor=date(2026, 7, 1),
                ),
                second.estimand_id: make_request(
                    frame,
                    anchor=date(2026, 7, 1),
                    expected="7",
                    observed="6",
                    valid="6",
                    invalid="0",
                    missing="1",
                ),
            }
        return self._finish_plan_bundle(
            case=case,
            frame=frame,
            requests_by_estimand_id=requests_by_estimand_id,
        )

    def test_user_correction_fences_persisted_measurement_derivations(
        self,
    ) -> None:
        bundle = self._build_plan_bundle()
        outcome = self.store.get_measurement_resolution(
            bundle.plan.resolution_outcome_ids[0]
        )
        admission = self.store.get_measurement_resolution_admission(
            outcome.resolution_outcome_id
        )
        obligation = self.store.get_evidence_obligation(
            bundle.adoption.obligation_ids[0]
        )
        snapshot = self.store.get_authority_snapshot(self.case_id)
        payload = {"message": "改用另一个业务时间口径"}
        self.store.append_mailbox_message(
            message_id=f"{self.case_id}:message:derivation-correction",
            case_id=self.case_id,
            kind=MailboxMessageKind.USER_CORRECTION,
            operation=OperationIdentity(
                operation_id=(
                    f"{self.case_id}:operation:derivation-correction"
                ),
                idempotency_key=(
                    f"{self.case_id}:derivation-correction-key"
                ),
                causation_id=f"{self.case_id}:user-turn:2",
                correlation_id=self.case_id,
                authority_revision=snapshot.mailbox_authority_epoch,
                payload_sha256=content_sha256(payload),
            ),
            payload=payload,
            created_at=NOW,
        )

        with self.assertRaisesRegex(
            StaleHead,
            "measurement derivation authority is stale",
        ):
            self.store.record_measurement_resolution(
                outcome,
                admission=admission,
                expected_head_version=snapshot.head_version,
                event_id=f"{self.case_id}:event:stale-resolution",
            )
        with self.assertRaisesRegex(
            StaleHead,
            "authority epoch changed before commit",
        ):
            self.store.record_measurement_resolution(
                outcome,
                admission=admission,
                expected_head_version=snapshot.head_version,
                event_id=(
                    f"{self.case_id}:event:stale-resolution-operation"
                ),
                operation=self.resolution_operation,
            )
        with self.assertRaisesRegex(
            StaleHead,
            "obligation derivation authority is stale",
        ):
            self.store.record_evidence_obligation(
                obligation,
                expected_head_version=snapshot.head_version,
                event_id=f"{self.case_id}:event:stale-obligation",
            )

    def test_outbox_commit_waits_for_mailbox_correction_fence(
        self,
    ) -> None:
        self._build_plan_bundle()
        snapshot = self.store.get_authority_snapshot(self.case_id)
        payload = {"projection": "refresh"}
        operation = OperationIdentity(
            operation_id=f"{self.case_id}:operation:projection",
            idempotency_key=f"{self.case_id}:projection-key",
            causation_id=f"{self.case_id}:event:frame",
            correlation_id=self.case_id,
            authority_revision=snapshot.mailbox_authority_epoch,
            payload_sha256=content_sha256(payload),
        )
        message = OutboxMessage(
            outbox_message_id=f"{self.case_id}:outbox:projection",
            case_id=self.case_id,
            source_event_cursor=self.store.list_events(self.case_id)[-1].cursor,
            action_id=None,
            job_kind=AsyncJobKind.PROJECTION,
            operation=operation,
            expected_head_version=snapshot.head_version,
            expected_authority_epoch=snapshot.mailbox_authority_epoch,
            authority_snapshot=snapshot,
            authority_snapshot_sha256=snapshot.content_sha256,
            idempotency_key=operation.idempotency_key,
            destination="projection-worker",
            contract_ref="waje-vnext://projection/refresh.v1",
            payload=payload,
            payload_sha256=content_sha256(payload),
            created_at=NOW,
        )
        assert DSN is not None
        blocker = psycopg.connect(DSN)
        worker = PostgresAuthorityStore.connect(DSN)
        worker_pid = worker._connection.info.backend_pid
        result: dict[str, object] = {}

        def enqueue() -> None:
            try:
                result["value"] = worker.enqueue_outbox(message)
            except Exception as error:
                result["error"] = error

        thread = threading.Thread(target=enqueue)
        try:
            with blocker.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT authority_epoch
                    FROM waje_vnext.case_mailbox_heads
                    WHERE case_id = %s
                    FOR UPDATE
                    """,
                    (self.case_id,),
                )
                cursor.execute(
                    """
                    UPDATE waje_vnext.case_mailbox_heads
                    SET authority_epoch = authority_epoch + 1
                    WHERE case_id = %s
                    """,
                    (self.case_id,),
                )
                thread.start()
                blocked = False
                for _ in range(100):
                    cursor.execute(
                        """
                        SELECT wait_event_type
                        FROM pg_stat_activity
                        WHERE pid = %s
                        """,
                        (worker_pid,),
                    )
                    row = cursor.fetchone()
                    if row is not None and row[0] == "Lock":
                        blocked = True
                        break
                    time.sleep(0.02)
                self.assertTrue(
                    blocked,
                    "outbox commit did not wait on the mailbox fence",
                )
            blocker.commit()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(result.get("error"), StaleHead)
            with self.assertRaises(AuthorityNotFound):
                self.store.get_outbox_message(
                    message.outbox_message_id
                )
        finally:
            blocker.rollback()
            blocker.close()
            worker.close()

    def _finish_plan_bundle(
        self,
        *,
        case,
        frame,
        requests_by_estimand_id=None,
    ) -> PlanBundle:
        context = make_context()
        resolver = make_trusted_resolver()
        derivation_authority = (
            MeasurementDerivationAuthority.from_authority_snapshot(
                self.store.get_authority_snapshot(self.case_id)
            )
        )
        resolved_entries = []
        for estimand in frame.measurement_design.estimands:
            request = (
                make_request(
                    frame,
                    anchor=date(2026, 7, 1),
                )
                if requests_by_estimand_id is None
                else requests_by_estimand_id[estimand.estimand_id]
            )
            registry = make_trusted_registry(request, context)
            outcome = resolver.resolve_measurement(
                frame=frame,
                derivation_authority=derivation_authority,
                estimand_id=estimand.estimand_id,
                context=context,
                request=request,
                trusted_input_registry=registry,
                created_at=NOW,
            )
            admission = resolver.admit_resolution(
                frame=frame,
                outcome=outcome,
                context=context,
                request=request,
                trusted_input_registry=registry,
            )
            resolved_entries.append(
                (
                    outcome,
                    admission,
                    resolver.compile_evidence_obligations(
                        frame=frame,
                        outcome=outcome,
                        context=context,
                        resolution_request=request,
                        trusted_input_registry=registry,
                        created_at=NOW,
                    ),
                )
            )
        outcomes = tuple(item[0] for item in resolved_entries)
        admissions = tuple(item[1] for item in resolved_entries)
        obligations = tuple(
            obligation
            for _, _, items in resolved_entries
            for obligation in items
        )
        self.resolution_operations = []
        for index, (outcome, admission, _) in enumerate(
            resolved_entries,
            start=1,
        ):
            operation = OperationIdentity(
                operation_id=(
                    f"{self.case_id}:operation:resolution:{index}"
                ),
                idempotency_key=(
                    f"{self.case_id}:resolution-key:{index}"
                ),
                causation_id=(
                    f"{self.case_id}:operation:frame-accepted"
                ),
                correlation_id=f"{self.case_id}:run:1",
                authority_revision=(
                    self.store.get_mailbox_head(
                        self.case_id
                    ).authority_epoch
                ),
                payload_sha256=outcome.content_sha256,
            )
            self.resolution_operations.append(operation)
            self.store.record_measurement_resolution(
                outcome,
                admission=admission,
                expected_head_version=case.head_version,
                event_id=f"{self.case_id}:event:resolution:{index}",
                operation=operation,
            )
        self.resolution_operation = self.resolution_operations[0]
        resolution_operation_by_outcome_id = {
            outcome.resolution_outcome_id: operation
            for (outcome, _, _), operation in zip(
                resolved_entries,
                self.resolution_operations,
                strict=True,
            )
        }
        self.obligation_operations = []
        for index, obligation in enumerate(obligations, start=1):
            operation = OperationIdentity(
                operation_id=(
                    f"{self.case_id}:operation:obligation:{index}"
                ),
                idempotency_key=(
                    f"{self.case_id}:obligation-key:{index}"
                ),
                causation_id=resolution_operation_by_outcome_id[
                    obligation.resolution_outcome_id
                ].operation_id,
                correlation_id=f"{self.case_id}:run:1",
                authority_revision=(
                    self.store.get_mailbox_head(
                        self.case_id
                    ).authority_epoch
                ),
                payload_sha256=obligation.content_sha256,
            )
            self.obligation_operations.append(operation)
            self.store.record_evidence_obligation(
                obligation,
                expected_head_version=case.head_version,
                event_id=(
                    f"{self.case_id}:event:obligation:{index}"
                ),
                operation=operation,
            )

        authority_snapshot = self.store.get_authority_snapshot(
            self.case_id
        )
        self.outcomes = outcomes
        self.admissions = admissions
        self.obligations = obligations
        return compile_plan_bundle(
            case=self.store.get_case(self.case_id),
            authority_snapshot=authority_snapshot,
            frame=frame,
            outcomes=outcomes,
            admissions=admissions,
            obligations=obligations,
            proposed_tasks=tuple(
                ProposedWorkTask(
                    proposal_task_key=f"close-obligation-{index}",
                    business_purpose=(
                        "Close one accepted evidence obligation"
                    ),
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        + (
                            "measurement-evidence.v1"
                            if obligation.execution_disposition
                            is ObligationExecutionDisposition.EXECUTABLE
                            else "boundary-inspection.v1"
                        )
                    ),
                    obligation_ids=(obligation.obligation_id,),
                    depends_on_task_keys=(),
                )
                for index, obligation in enumerate(
                    obligations,
                    start=1,
                )
            ),
            plan_revision_id=f"{self.case_id}:plan:1",
            revision_number=1,
            prior_plan_revision_id=None,
            created_by_action_id=f"{self.case_id}:action:plan:1",
            created_at=NOW,
            revision_reason="Adopt resolved measurement obligations",
        )

    def _compile_competing_bundle(
        self,
        *,
        plan_revision_id: str,
    ) -> PlanBundle:
        return compile_plan_bundle(
            case=self.store.get_case(self.case_id),
            authority_snapshot=self.store.get_authority_snapshot(
                self.case_id
            ),
            frame=self.frame,
            outcomes=self.outcomes,
            admissions=self.admissions,
            obligations=self.obligations,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="competing-plan-task",
                    business_purpose="Compete for the same accepted head",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in self.obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
            plan_revision_id=plan_revision_id,
            revision_number=1,
            prior_plan_revision_id=None,
            created_by_action_id=f"{self.case_id}:action:competing-plan",
            created_at=NOW,
            revision_reason="Competing valid plan",
        )

    def _plan_exists(self, plan_revision_id: str) -> bool:
        try:
            self.store.get_plan(plan_revision_id)
        except AuthorityNotFound:
            return False
        return True


if __name__ == "__main__":
    unittest.main()
