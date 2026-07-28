from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.durable_call_journal import DurableCallSpec
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_digest
from bi_agent.runtime.single_authority import (
    DurableTransition,
    FailureRecord,
    IntentRevision,
)


QUESTION = "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"


def _accepted_provider_attempt_ref(
    store: PostgresConversationStore,
    *,
    run_id: str,
    intent_revision_id: str | None,
    call_kind: str,
    stage_name: str,
) -> str:
    input_payload = {
        "test_provider_call": stage_name,
        "run_attempt_id": run_id,
    }
    input_digest = canonical_digest(input_payload)
    spec = DurableCallSpec.create(
        run_attempt_id=run_id,
        intent_revision_id=intent_revision_id,
        plan_revision_id=None,
        task_id=None,
        stage_name=stage_name,
        call_kind=call_kind,
        operation_name=f"test_{stage_name}",
        input_ref="provider-call-input:sha256:" + input_digest,
        input_payload=input_payload,
    )
    claim = store.attempt_journal.claim(spec)
    if claim.replayed:
        return claim.attempt.attempt_ref
    completion = store.attempt_journal.succeed(
        claim.attempt,
        {
            "output": {"accepted": True},
            "audit": {"task": f"test_{stage_name}"},
        },
    )
    assert completion.acceptance is not None
    return completion.acceptance.accepted_attempt_ref


def _revision(
    run_id: str,
    *,
    target: str = "2026-06-01",
    supersedes: str | None = None,
) -> IntentRevision:
    metric = "付费金额"
    date_text = "2026年6月1日"
    return IntentRevision.create(
        run_attempt_id=run_id,
        supersedes_intent_revision_id=supersedes,
        original_user_text=QUESTION,
        business_summary="你希望分析2026年6月1日全量样本付费金额上涨及其主要驱动。",
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec={"kind": "date", "target": target},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_baseline",
        },
        direction_premise="user_hypothesis_positive",
        requested_factor_refs=(),
        requested_analysis_axes=("formula_tree", "dimension_screen", "time_context"),
        desired_decisions=(
            {"decision_kind": "explain_change", "target_ref": "paid_amount"},
        ),
        ambiguity_slots=(
            {
                "slot_id": "comparison_baseline",
                "slot_kind": "baseline",
                "materiality": "material",
                "status": "unresolved",
                "question": "目标日期要与哪个基准比较？",
                "allowed_value_refs": [
                    "previous_day",
                    "rolling_7_day_baseline",
                    "same_weekday_last_week",
                ],
            },
        ),
        source_spans=(
            {
                "field": "target_metric_refs[0]",
                "start": QUESTION.index(metric),
                "end": QUESTION.index(metric) + len(metric),
                "text": metric,
            },
            {
                "field": "time_spec.target",
                "start": QUESTION.index(date_text),
                "end": QUESTION.index(date_text) + len(date_text),
                "text": date_text,
            },
        ),
        schema_version="intent-revision.v3",
        prompt_version="single-authority.phase01.test.v1",
        model_version="deterministic-contract-record",
        known_goal_ids={"explain_change"},
        known_metric_ids={"paid_amount"},
        known_analysis_axis_ids={"formula_tree", "dimension_screen", "time_context"},
        known_scope_types={"full_sample"},
        known_ambiguity_value_refs={
            "previous_day",
            "rolling_7_day_baseline",
            "same_weekday_last_week",
        },
    )


def _transition(
    *,
    node: str,
    run_id: str,
    revision_id: str,
    position: int,
    input_payload: dict,
    output_payload: dict,
    parent: str | None = None,
    next_transition: str,
) -> DurableTransition:
    return DurableTransition.create(
        node_name=node,
        parent_transition_id=parent,
        run_attempt_id=run_id,
        intent_revision_id=revision_id,
        decision_ledger_position=position,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref="deterministic_contract_test",
        model_ref="typed_record",
        status="succeeded",
        acceptance_state="accepted",
        next_transition=next_transition,
    )


class SingleAuthorityPostgresIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")):
            raise unittest.SkipTest("runtime PostgreSQL is not configured")
        cls.store = PostgresConversationStore.from_env()
        cls.store.apply_schema()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "store"):
            cls.store.connection.close()

    def _accepted_revision(
        self, run_id: str
    ) -> tuple[IntentRevision, DurableTransition]:
        self.store.upsert_run(run_id, thread_id=self.thread_id, status="running")
        revision = _revision(run_id)
        input_payload = {"question": QUESTION, "contract_test": True}
        output_payload = {"intent_revision": revision.to_dict()}
        transition = _transition(
            node="bind_intent",
            run_id=run_id,
            revision_id=revision.intent_revision_id,
            position=0,
            input_payload=input_payload,
            output_payload=output_payload,
            next_transition="resolve_material_decisions",
        )
        self.store.save_intent_revision_transition(
            intent_revision=revision,
            transition=transition,
            input_payload=input_payload,
            output_payload=output_payload,
            accepted_attempt_refs=(
                _accepted_provider_attempt_ref(
                    self.store,
                    run_id=run_id,
                    intent_revision_id=None,
                    call_kind="intent_provider",
                    stage_name="bind_intent",
                ),
            ),
        )
        return revision, transition

    def setUp(self):
        suffix = uuid4().hex
        self.thread_id = f"phase01-contract-thread-{suffix}"
        self.store.create_thread(self.thread_id, owner_id="phase01-contract-user")

    def test_accepted_intent_projects_its_business_summary_into_the_run(self):
        run_id = f"phase01-contract-run-{uuid4().hex}"
        revision, _ = self._accepted_revision(run_id)

        run_state = self.store.get_run_state(run_id)
        request = run_state["request"]
        self.assertEqual(
            request["business_understanding"],
            revision.business_summary,
        )
        self.assertEqual(
            request["business_understanding_intent_revision_id"],
            revision.intent_revision_id,
        )

    def test_waiting_transition_is_atomic_and_replayable(self):
        run_id = f"phase01-contract-run-{uuid4().hex}"
        revision, intent_transition = self._accepted_revision(run_id)
        options = [
            {
                "slot_id": "comparison_baseline",
                "option_id": "comparison_baseline.previous_day",
                "typed_value": {"baseline_id": "previous_day"},
                "display_label": "跟前一天比较（推荐）",
                "display_description": "比较目标日期与前一天。",
                "recommended": True,
            },
            {
                "slot_id": "comparison_baseline",
                "option_id": "comparison_baseline.same_weekday_last_week",
                "typed_value": {"baseline_id": "same_weekday_last_week"},
                "display_label": "跟上周同一天比较",
                "display_description": "比较目标日期与上周同一天。",
                "recommended": False,
            },
        ]
        option_input = {"intent_revision_id": revision.intent_revision_id}
        option_output = {"options": options}
        option_transition = _transition(
            node="generate_clarification",
            run_id=run_id,
            revision_id=revision.intent_revision_id,
            position=0,
            input_payload=option_input,
            output_payload=option_output,
            parent=intent_transition.transition_id,
            next_transition="persist_waiting_for_decision",
        )
        self.store.save_decision_options_transition(
            intent_revision_id=revision.intent_revision_id,
            options=options,
            transition=option_transition,
            input_payload=option_input,
            output_payload=option_output,
            accepted_attempt_refs=(
                _accepted_provider_attempt_ref(
                    self.store,
                    run_id=run_id,
                    intent_revision_id=revision.intent_revision_id,
                    call_kind="clarification_provider",
                    stage_name="generate_clarification",
                ),
            ),
        )
        lifecycle = self.store.latest_lifecycle_state(run_id)
        waiting_lifecycle = lifecycle.transition(
            execution_state="waiting",
            interaction_state="waiting_for_user",
        )
        waiting_input = {
            "intent_revision_id": revision.intent_revision_id,
            "decision_ledger_position": 0,
            "decision_options_digest": canonical_digest(options),
            "clarification_digest": canonical_digest(
                {"slot_id": "comparison_baseline"}
            ),
            "parent_transition_id": option_transition.transition_id,
        }
        waiting_output = {
            "status": "waiting_for_clarification",
            "lifecycle_state": waiting_lifecycle.to_dict(),
        }
        waiting_transition = _transition(
            node="persist_waiting_for_decision",
            run_id=run_id,
            revision_id=revision.intent_revision_id,
            position=0,
            input_payload=waiting_input,
            output_payload=waiting_output,
            parent=option_transition.transition_id,
            next_transition="await_user_decision",
        )

        with patch.object(
            self.store,
            "_save_transition_attempt_locked",
            side_effect=RuntimeError("injected_transition_write_failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "injected_transition_write_failure"
            ):
                self.store.save_waiting_transition(
                    transition=waiting_transition,
                    lifecycle=waiting_lifecycle,
                    input_payload=waiting_input,
                    output_payload=waiting_output,
                )
        self.assertEqual(self.store.latest_lifecycle_state(run_id), lifecycle)

        inserted = self.store.save_waiting_transition(
            transition=waiting_transition,
            lifecycle=waiting_lifecycle,
            input_payload=waiting_input,
            output_payload=waiting_output,
        )
        replayed = self.store.save_waiting_transition(
            transition=waiting_transition,
            lifecycle=waiting_lifecycle,
            input_payload=waiting_input,
            output_payload=waiting_output,
        )

        self.assertEqual(inserted, "inserted")
        self.assertEqual(replayed, "replayed")
        self.assertEqual(self.store.latest_lifecycle_state(run_id), waiting_lifecycle)
        accepted = self.store.load_accepted_transition(
            run_attempt_id=run_id,
            node_name="persist_waiting_for_decision",
            input_digest=canonical_digest(waiting_input),
        )
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["transition"].next_transition, "await_user_decision")
        counts = self.store._fetchone(
            """
            SELECT
              (SELECT count(*)
               FROM waje_runtime.workflow_transition_attempts
               WHERE run_attempt_id = %(run_id)s
                 AND node_name = 'persist_waiting_for_decision'
                 AND acceptance_state = 'accepted'),
              (SELECT count(*)
               FROM waje_runtime.run_lifecycle_state_revisions
               WHERE run_attempt_id = %(run_id)s
                 AND execution_state = 'waiting'
                 AND interaction_state = 'waiting_for_user')
            """,
            {"run_id": run_id},
        )
        self.assertEqual(tuple(counts), (1, 1))

    def test_decision_resume_supersession_cancellation_and_publication_fence(self):
        original_run = f"phase01-contract-run-{uuid4().hex}"
        original, intent_transition = self._accepted_revision(original_run)
        options = [
            {
                "slot_id": "comparison_baseline",
                "option_id": "comparison_baseline.previous_day",
                "typed_value": {"baseline_id": "previous_day"},
                "display_label": "跟前一天比较（推荐）",
                "display_description": "比较目标日期与前一天。",
                "recommended": True,
            },
            {
                "slot_id": "comparison_baseline",
                "option_id": "comparison_baseline.same_weekday_last_week",
                "typed_value": {"baseline_id": "same_weekday_last_week"},
                "display_label": "跟上周同一天比较",
                "display_description": "比较目标日期与上周同一天。",
                "recommended": False,
            },
        ]
        option_input = {"intent_revision_id": original.intent_revision_id}
        option_output = {"options": options}
        option_transition = _transition(
            node="generate_clarification",
            run_id=original_run,
            revision_id=original.intent_revision_id,
            position=0,
            input_payload=option_input,
            output_payload=option_output,
            parent=intent_transition.transition_id,
            next_transition="wait_for_material_decision",
        )
        self.store.save_decision_options_transition(
            intent_revision_id=original.intent_revision_id,
            options=options,
            transition=option_transition,
            input_payload=option_input,
            output_payload=option_output,
            accepted_attempt_refs=(
                _accepted_provider_attempt_ref(
                    self.store,
                    run_id=original_run,
                    intent_revision_id=original.intent_revision_id,
                    call_kind="clarification_provider",
                    stage_name="generate_clarification",
                ),
            ),
        )

        def accept_once() -> dict:
            worker = PostgresConversationStore.from_env()
            try:
                return worker.accept_decision_option(
                    run_attempt_id=original_run,
                    option_id="comparison_baseline.previous_day",
                )
            finally:
                worker.connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = list(pool.map(lambda _: accept_once(), range(2)))
        self.assertEqual(
            {item["decision"]["decision_id"] for item in accepted},
            {accepted[0]["decision"]["decision_id"]},
        )
        self.assertEqual(
            {item["durable_checkpoint"]["transition_id"] for item in accepted},
            {accepted[0]["durable_checkpoint"]["transition_id"]},
        )
        counts = self.store._fetchone(
            """
            SELECT
              (SELECT count(*) FROM waje_runtime.intent_revisions
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.decision_records
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.workflow_transition_attempts
               WHERE run_attempt_id = %(run_id)s
                 AND node_name = 'accept_material_decision'
                 AND acceptance_state = 'accepted')
            """,
            {"run_id": original_run},
        )
        self.assertEqual(tuple(counts), (1, 1, 1))
        resumed_intent = self.store.load_accepted_transition(
            run_attempt_id=original_run,
            node_name="bind_intent",
            input_digest=canonical_digest(
                {"question": QUESTION, "contract_test": True}
            ),
        )
        self.assertIsNotNone(resumed_intent)
        self.assertEqual(
            resumed_intent["transition"].transition_id,
            intent_transition.transition_id,
        )
        corrected_run = f"phase01-contract-run-{uuid4().hex}"
        self.store.upsert_run(
            corrected_run,
            thread_id=self.thread_id,
            status="running",
        )
        corrected = _revision(
            corrected_run,
            target="2026-06-02",
            supersedes=original.intent_revision_id,
        )
        correction_input = {"question": QUESTION, "correction": "target_date"}
        correction_output = {"intent_revision": corrected.to_dict()}
        correction_transition = _transition(
            node="bind_intent",
            run_id=corrected_run,
            revision_id=corrected.intent_revision_id,
            position=2,
            input_payload=correction_input,
            output_payload=correction_output,
            next_transition="resolve_material_decisions",
        )
        self.store.save_intent_revision_transition(
            intent_revision=corrected,
            transition=correction_transition,
            input_payload=correction_input,
            output_payload=correction_output,
            accepted_attempt_refs=(
                _accepted_provider_attempt_ref(
                    self.store,
                    run_id=corrected_run,
                    intent_revision_id=None,
                    call_kind="intent_provider",
                    stage_name="bind_intent",
                ),
            ),
            affected_plan_fields=("resolved_window_refs", "time_spec"),
            reason_ref="typed_material_correction",
        )
        corrected_ledger = self.store.load_decision_ledger(corrected.intent_revision_id)
        self.assertEqual(corrected_ledger.position, 2)
        self.assertIsNone(corrected_ledger.active_for_slot("comparison_baseline"))
        self.assertIsNone(self.store.resolve_active_intent_revision(original_run))
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "publication_authority_missing|publication_revision_not_active",
        ):
            self.store.assert_revision_can_publish(
                run_attempt_id=original_run,
                intent_revision_id=original.intent_revision_id,
            )
        self.assertEqual(
            self.store.record_orphaned_result(
                run_attempt_id=original_run,
                result_intent_revision_id=original.intent_revision_id,
                active_intent_revision_id=corrected.intent_revision_id,
                source_transition_id=accepted[0]["durable_checkpoint"]["transition_id"],
                result_ref=f"late-result-{uuid4().hex}",
                reason="superseded_revision",
                payload={"accepted_evidence": False},
            ),
            "inserted",
        )

        cancelled_run = f"phase01-contract-run-{uuid4().hex}"
        cancelled, cancelled_transition = self._accepted_revision(cancelled_run)
        cancellation = self.store.cancel_run_attempt(
            run_attempt_id=cancelled_run,
            reason_ref="typed_user_cancellation",
        )
        self.assertEqual(cancellation["lifecycle"]["cancellation_state"], "cancelled")
        with self.assertRaisesRegex(
            EvidenceIntegrityError, "publication_revision_not_active"
        ):
            self.store.assert_revision_can_publish(
                run_attempt_id=cancelled_run,
                intent_revision_id=cancelled.intent_revision_id,
            )
        self.assertEqual(
            self.store.record_orphaned_result(
                run_attempt_id=cancelled_run,
                result_intent_revision_id=cancelled.intent_revision_id,
                active_intent_revision_id=cancelled.intent_revision_id,
                source_transition_id=cancelled_transition.transition_id,
                result_ref=f"cancelled-late-result-{uuid4().hex}",
                reason="cancelled_inflight_result",
                payload={"accepted_evidence": False},
            ),
            "inserted",
        )

        failure_a = FailureRecord.create(
            layer="capability",
            kind="auxiliary_branch_unavailable",
            scope="claim",
            affected_refs=("claim:auxiliary",),
            integrity_level="local",
            retryability="retryable",
            user_actionable=False,
            business_boundary="辅助说明暂不可用",
            technical_detail_ref="provider-detail-a",
        )
        failure_b = FailureRecord.create(
            layer="capability",
            kind="auxiliary_branch_unavailable",
            scope="claim",
            affected_refs=("claim:auxiliary",),
            integrity_level="local",
            retryability="retryable",
            user_actionable=False,
            business_boundary="附加信息稍后补充",
            technical_detail_ref="provider-detail-b",
        )
        self.assertEqual(failure_a.policy_scope, failure_b.policy_scope)


if __name__ == "__main__":
    unittest.main()
