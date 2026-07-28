from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest

from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    DurableTransition,
    FailureRecord,
    IntentRevision,
    InteractionDirective,
    LifecycleState,
    SingleAuthorityContractError,
    result_acceptance_state,
)


GOAL_IDS = {"explain_change", "validate_change"}
METRIC_IDS = {"paid_amount", "payer_count"}
AXIS_IDS = {"formula_tree", "dimension_screen", "time_context"}
ROOT = Path(__file__).resolve().parents[2]


def intent_revision(
    *,
    original_user_text: str = "2026年6月1日付费金额为什么上涨？",
    business_summary: str = "你希望分析2026年6月1日付费金额上涨的业务驱动。",
    supersedes_intent_revision_id: str | None = None,
    requested_factor_refs: tuple[str, ...] = (),
) -> IntentRevision:
    metric_text = "付费金额"
    metric_start = original_user_text.index(metric_text)
    date_text = "2026年6月1日"
    date_start = original_user_text.index(date_text)
    return IntentRevision.create(
        run_attempt_id="run-attempt-1",
        supersedes_intent_revision_id=supersedes_intent_revision_id,
        original_user_text=original_user_text,
        business_summary=business_summary,
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec={"kind": "date", "target": "2026-06-01"},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_baseline",
        },
        direction_premise="user_hypothesis_positive",
        requested_factor_refs=requested_factor_refs,
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
                "start": metric_start,
                "end": metric_start + len(metric_text),
                "text": metric_text,
            },
            {
                "field": "time_spec.target",
                "start": date_start,
                "end": date_start + len(date_text),
                "text": date_text,
            },
        ),
        schema_version="intent-revision.v3",
        prompt_version="single-authority-intent.v2",
        model_version="deepseek-v4-flash",
        known_goal_ids=GOAL_IDS,
        known_metric_ids=METRIC_IDS,
        known_analysis_axis_ids=AXIS_IDS,
    )


class IntentRevisionContractTest(unittest.TestCase):
    def test_intent_revision_is_immutable_and_content_addressed(self):
        revision = intent_revision()

        with self.assertRaises(FrozenInstanceError):
            revision.scope = {"scope_type": "segment", "filters": []}

        self.assertTrue(revision.intent_revision_id.startswith("intent-revision-"))
        self.assertEqual(len(revision.content_digest), 64)
        self.assertEqual(IntentRevision.from_dict(revision.to_dict()), revision)

        tampered = revision.to_dict()
        tampered["time_spec"] = {"kind": "date", "target": "2026-06-02"}
        with self.assertRaisesRegex(
            SingleAuthorityContractError, "intent_revision_content_digest_invalid"
        ):
            IntentRevision.from_dict(tampered)

    def test_paraphrases_keep_material_binding_digest_stable(self):
        first = intent_revision()
        second = intent_revision(
            original_user_text="请分析2026年6月1日付费金额上升的主要驱动。",
            business_summary="你希望定位2026年6月1日付费金额上升的主要驱动。",
        )

        self.assertNotEqual(first.content_digest, second.content_digest)
        self.assertEqual(first.material_binding_digest, second.material_binding_digest)

    def test_business_summary_is_part_of_the_revision_but_not_plan_materiality(self):
        first = intent_revision()
        second = intent_revision(
            business_summary="你希望解释2026年6月1日付费金额上升的业务原因。"
        )

        self.assertNotEqual(first.content_digest, second.content_digest)
        self.assertNotEqual(first.intent_revision_id, second.intent_revision_id)
        self.assertEqual(first.material_binding_digest, second.material_binding_digest)
        self.assertEqual(
            IntentRevision.from_dict(second.to_dict()).business_summary,
            second.business_summary,
        )

    def test_requested_factor_refs_are_typed_material_intent(self):
        general = intent_revision()
        focused = intent_revision(requested_factor_refs=("payer_count",))

        self.assertEqual(focused.requested_factor_refs, ("payer_count",))
        self.assertNotEqual(
            general.material_binding_digest,
            focused.material_binding_digest,
        )

        payload = focused.to_dict()
        payload.pop("intent_revision_id")
        payload.pop("content_digest")
        payload["requested_factor_refs"] = ["unknown_factor"]
        with self.assertRaisesRegex(
            SingleAuthorityContractError,
            "intent_revision_factor_ref_unknown",
        ):
            IntentRevision.create(**payload, known_metric_ids=METRIC_IDS)

        payload["requested_factor_refs"] = ["paid_amount"]
        with self.assertRaisesRegex(
            SingleAuthorityContractError,
            "intent_revision_factor_target_overlap_invalid",
        ):
            IntentRevision.create(**payload, known_metric_ids=METRIC_IDS)

    def test_source_span_must_belong_to_original_user_text(self):
        revision = intent_revision()
        payload = revision.to_dict()
        payload.pop("intent_revision_id")
        payload.pop("content_digest")
        payload["source_spans"][0]["text"] = "付款金额"

        with self.assertRaisesRegex(
            SingleAuthorityContractError, "intent_revision_source_span_invalid"
        ):
            IntentRevision.create(**payload)

    def test_time_spec_union_rejects_extra_keys(self):
        revision = intent_revision()
        payload = revision.to_dict()
        payload.pop("intent_revision_id")
        payload.pop("content_digest")
        payload["time_spec"]["timezone"] = "UTC"

        with self.assertRaisesRegex(
            SingleAuthorityContractError,
            "intent_revision_time_spec_invalid",
        ):
            IntentRevision.create(**payload)

    def test_explicit_comparison_cannot_keep_a_comparison_decision_slot(self):
        revision = intent_revision()
        payload = revision.to_dict()
        payload.pop("intent_revision_id")
        payload.pop("content_digest")
        payload["comparison_spec"] = {
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-05-31",
            "baseline_end": "2026-05-31",
            "aggregation": "sum_of_complete_days",
        }

        with self.assertRaisesRegex(
            SingleAuthorityContractError,
            "intent_revision_comparison_authority_invalid",
        ):
            IntentRevision.create(**payload)

    def test_provider_cannot_supply_internal_authority_ids(self):
        provider_output = {
            "intent_revision_id": "forged",
            "run_attempt_id": "forged",
            "goal_bindings": [{"goal_id": "explain_change", "role": "primary"}],
            "target_metric_refs": ["paid_amount"],
            "scope": {"scope_type": "full_sample", "filters": []},
            "time_spec": {"kind": "date", "target": "2026-06-01"},
            "comparison_spec": {"kind": "none"},
            "direction_premise": "user_hypothesis_positive",
            "requested_analysis_axes": ["formula_tree"],
            "requested_factor_refs": [],
            "desired_decisions": [],
            "ambiguity_slots": [],
            "source_spans": [],
        }

        with self.assertRaisesRegex(
            SingleAuthorityContractError, "intent_binding_provider_shape_invalid"
        ):
            IntentRevision.from_provider_binding(
                provider_output,
                run_attempt_id="run-attempt-1",
                original_user_text="2026年6月1日付费金额为什么上涨？",
                business_summary="你希望分析2026年6月1日付费金额上涨的业务驱动。",
                schema_version="intent-revision.v3",
                prompt_version="single-authority-intent.v2",
                model_version="deepseek-v4-flash",
                known_goal_ids=GOAL_IDS,
                known_metric_ids=METRIC_IDS,
                known_analysis_axis_ids=AXIS_IDS,
            )

    def test_provider_scope_filter_must_use_customer_safe_contract_field(self):
        provider_output = {
            "goal_bindings": [{"goal_id": "explain_change", "role": "primary"}],
            "target_metric_refs": ["paid_amount"],
            "scope": {
                "scope_type": "full_sample",
                "filters": [{"field": "user_id", "op": "eq", "value": "u-00042"}],
            },
            "time_spec": {"kind": "date", "target": "2026-06-01"},
            "comparison_spec": {"kind": "none"},
            "direction_premise": "user_hypothesis_positive",
            "requested_analysis_axes": ["formula_tree"],
            "requested_factor_refs": [],
            "desired_decisions": [],
            "ambiguity_slots": [],
            "source_spans": [],
        }

        with self.assertRaisesRegex(
            SingleAuthorityContractError,
            "intent_revision_scope_filter_field_unapproved",
        ):
            IntentRevision.from_provider_binding(
                provider_output,
                run_attempt_id="run-attempt-1",
                original_user_text="分析用户 u-00042 的付费金额",
                business_summary="你希望分析指定用户的付费金额。",
                schema_version="intent-revision.v3",
                prompt_version="single-authority-intent.v2",
                model_version="deepseek-v4-flash",
                known_goal_ids=GOAL_IDS,
                known_metric_ids=METRIC_IDS,
                known_analysis_axis_ids=AXIS_IDS,
                known_scope_types={"full_sample"},
                known_filter_fields={"channel"},
            )


class DecisionLedgerContractTest(unittest.TestCase):
    def test_same_option_id_is_idempotent(self):
        revision = intent_revision()
        decision = DecisionRecord.create(
            intent_revision_id=revision.intent_revision_id,
            slot_id="comparison_baseline",
            value={"baseline_id": "previous_day"},
            source="user",
            status="user_confirmed",
            materiality="material",
            affected_plan_fields=("baseline_refs", "resolved_window_refs"),
            option_id="baseline.previous_day",
        )
        duplicate = DecisionRecord.create(
            intent_revision_id=revision.intent_revision_id,
            slot_id="comparison_baseline",
            value={"baseline_id": "previous_day"},
            source="user",
            status="user_confirmed",
            materiality="material",
            affected_plan_fields=("baseline_refs", "resolved_window_refs"),
            option_id="baseline.previous_day",
        )

        ledger = DecisionLedger().append(decision).append(duplicate)

        self.assertEqual(len(ledger.records), 1)
        self.assertEqual(
            ledger.active_for_slot("comparison_baseline").decision_id,
            decision.decision_id,
        )

    def test_same_option_id_with_different_value_is_rejected(self):
        revision = intent_revision()
        first = DecisionRecord.create(
            intent_revision_id=revision.intent_revision_id,
            slot_id="comparison_baseline",
            value={"baseline_id": "previous_day"},
            source="user",
            status="user_confirmed",
            materiality="material",
            affected_plan_fields=("baseline_refs",),
            option_id="baseline.previous_day",
        )
        forged = DecisionRecord.create(
            intent_revision_id=revision.intent_revision_id,
            slot_id="comparison_baseline",
            value={"baseline_id": "same_weekday_last_week"},
            source="user",
            status="user_confirmed",
            materiality="material",
            affected_plan_fields=("baseline_refs",),
            option_id="baseline.previous_day",
        )

        with self.assertRaisesRegex(
            SingleAuthorityContractError, "decision_option_id_conflict"
        ):
            DecisionLedger().append(first).append(forged)

    def test_supersession_is_append_only_and_scoped(self):
        revision = intent_revision()
        baseline = DecisionRecord.create(
            intent_revision_id=revision.intent_revision_id,
            slot_id="comparison_baseline",
            value={"baseline_id": "previous_day"},
            source="user",
            status="user_confirmed",
            materiality="material",
            affected_plan_fields=("baseline_refs", "resolved_window_refs"),
            option_id="baseline.previous_day",
        )
        display = DecisionRecord.create(
            intent_revision_id=revision.intent_revision_id,
            slot_id="display_density",
            value={"density": "concise"},
            source="safe_inference",
            status="inferred",
            materiality="non_material",
            affected_plan_fields=("display_policy",),
        )
        next_revision = intent_revision(
            supersedes_intent_revision_id=revision.intent_revision_id
        )

        ledger = DecisionLedger().append(baseline).append(display)
        superseded = ledger.supersede_for_revision(
            next_revision.intent_revision_id,
            affected_plan_fields={"baseline_refs", "resolved_window_refs"},
        )

        self.assertEqual(len(ledger.records), 2)
        self.assertIsNone(superseded.active_for_slot("comparison_baseline"))
        self.assertEqual(
            superseded.active_for_slot("display_density").value,
            {"density": "concise"},
        )
        invalidation = superseded.records[-2]
        self.assertEqual(invalidation.status, "invalidated")
        self.assertEqual(
            invalidation.invalidated_by_revision_id,
            next_revision.intent_revision_id,
        )


class FailureLifecycleAndTransitionContractTest(unittest.TestCase):
    def test_failure_scope_does_not_depend_on_technical_wording(self):
        first = FailureRecord.create(
            layer="capability",
            kind="provider_unavailable",
            scope="task",
            affected_refs=("task-payment-success",),
            integrity_level="none",
            retryability="retryable",
            user_actionable=False,
            business_boundary="支付成功率证据暂不可用，不影响付费金额主结论。",
            technical_detail_ref="detail-timeout-v1",
        )
        second = FailureRecord.create(
            layer="capability",
            kind="provider_unavailable",
            scope="task",
            affected_refs=("task-payment-success",),
            integrity_level="none",
            retryability="retryable",
            user_actionable=False,
            business_boundary="支付成功率证据暂不可用，不影响付费金额主结论。",
            technical_detail_ref="detail-provider-message-changed",
        )

        self.assertEqual(first.policy_scope, second.policy_scope)
        self.assertNotEqual(first.content_digest, second.content_digest)

    def test_cancelled_or_superseded_results_are_orphaned(self):
        active = LifecycleState.create(run_attempt_id="run-attempt-1")
        cancelled = active.transition(
            interaction_state="closed",
            cancellation_state="cancelled",
            execution_state="cancelled",
            publication_state="not_ready",
        )

        self.assertEqual(
            result_acceptance_state(
                lifecycle=cancelled,
                result_intent_revision_id="intent-old",
                active_intent_revision_id="intent-old",
            ),
            "orphaned",
        )
        self.assertEqual(
            result_acceptance_state(
                lifecycle=active,
                result_intent_revision_id="intent-old",
                active_intent_revision_id="intent-new",
            ),
            "orphaned",
        )

    def test_transition_attempts_share_identity_but_only_one_is_accepted(self):
        first = DurableTransition.create(
            node_name="bind_intent",
            parent_transition_id=None,
            run_attempt_id="run-attempt-1",
            intent_revision_id="",
            decision_ledger_position=0,
            input_digest="a" * 64,
            output_digest="b" * 64,
            execution_attempt=1,
            provider_ref="deepseek",
            model_ref="deepseek-v4-flash",
            status="succeeded",
            acceptance_state="accepted",
            next_transition="resolve_material_decisions",
        )
        retry = DurableTransition.create(
            node_name="bind_intent",
            parent_transition_id=None,
            run_attempt_id="run-attempt-1",
            intent_revision_id="",
            decision_ledger_position=0,
            input_digest="a" * 64,
            output_digest="c" * 64,
            execution_attempt=2,
            provider_ref="deepseek",
            model_ref="deepseek-v4-flash",
            status="succeeded",
            acceptance_state="rejected",
            next_transition="resolve_material_decisions",
        )

        self.assertEqual(first.transition_id, retry.transition_id)
        self.assertNotEqual(first.attempt_id, retry.attempt_id)
        self.assertEqual(first.acceptance_state, "accepted")
        self.assertEqual(retry.acceptance_state, "rejected")

    def test_transition_timestamps_use_database_stable_utc_form(self):
        transition = DurableTransition.create(
            node_name="execute_capability_dag",
            parent_transition_id="transition-parent",
            run_attempt_id="run-attempt-1",
            intent_revision_id="intent-revision-1",
            decision_ledger_position=1,
            input_digest="a" * 64,
            output_digest="b" * 64,
            execution_attempt=1,
            provider_ref="waje-capability-runtime",
            model_ref="deterministic-capability-dag.v1",
            status="succeeded",
            acceptance_state="accepted",
            next_transition="phase03_evidence_bound",
            started_at="2026-07-18T18:34:44.644580+08:00",
            finished_at="2026-07-18T10:34:44.644580Z",
        )

        self.assertEqual(
            transition.started_at,
            "2026-07-18T10:34:44.64458+00:00",
        )
        self.assertEqual(
            transition.finished_at,
            "2026-07-18T10:34:44.64458+00:00",
        )
        self.assertEqual(DurableTransition.from_dict(transition.to_dict()), transition)

    def test_interaction_directive_is_typed_and_content_addressed(self):
        directive = InteractionDirective.create(
            run_attempt_id="run-attempt-1",
            intent_revision_id="intent-revision-1",
            kind="challenge",
            target_refs=("decision-1",),
            original_user_text="这个基线选择的依据够吗？",
        )

        self.assertTrue(directive.directive_id.startswith("directive-"))
        self.assertEqual(InteractionDirective.from_dict(directive.to_dict()), directive)


class Phase01PostgresSchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
            encoding="utf-8"
        )
        cls.store = (ROOT / "bi_agent/conversation/postgres_store.py").read_text(
            encoding="utf-8"
        )

    def test_schema_has_single_authority_records_and_migration_ledger(self):
        for table in (
            "schema_migrations",
            "intent_revisions",
            "intent_revision_supersessions",
            "decision_options",
            "decision_records",
            "workflow_transition_attempts",
            "failure_records",
            "interaction_directives",
            "run_lifecycle_state_revisions",
            "orphaned_results",
        ):
            self.assertIn(f"waje_runtime.{table}", self.schema)

    def test_schema_enforces_idempotency_and_one_accepted_attempt(self):
        self.assertIn("idx_decision_records_option_idempotency", self.schema)
        self.assertIn("idx_workflow_transition_one_accepted", self.schema)
        self.assertIn("WHERE acceptance_state = 'accepted'", self.schema)
        self.assertIn("UNIQUE(run_attempt_id, content_digest)", self.schema)
        transition_table_start = self.schema.index(
            "CREATE TABLE IF NOT EXISTS waje_runtime.workflow_transition_attempts"
        )
        transition_table_end = self.schema.index(");", transition_table_start)
        transition_table = self.schema[transition_table_start:transition_table_end]
        self.assertNotIn("UNIQUE(run_attempt_id),", transition_table)

    def test_authority_records_are_append_only(self):
        self.assertIn("reject_append_only_authority_mutation", self.schema)
        for trigger in (
            "intent_revisions_append_only",
            "intent_revision_supersessions_append_only",
            "decision_options_append_only",
            "decision_records_append_only",
            "failure_records_append_only",
            "interaction_directives_append_only",
            "run_lifecycle_state_revisions_append_only",
            "orphaned_results_append_only",
        ):
            self.assertIn(trigger, self.schema)

    def test_store_exposes_atomic_intent_decision_and_transition_operations(self):
        for method in (
            "save_intent_revision_transition",
            "resolve_active_intent_revision",
            "save_decision_options_transition",
            "save_waiting_transition",
            "append_decision_record_transition",
            "load_decision_ledger",
            "load_accepted_transition",
            "latest_accepted_transition_id",
            "load_accepted_free_text_submission",
            "record_typed_slot_decision",
            "save_interaction_directive_transition",
            "save_failure_record",
            "append_lifecycle_state",
            "record_orphaned_result",
            "assert_revision_can_publish",
        ):
            self.assertIn(f"def {method}(", self.store)


class Phase01WorkflowWiringContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / "bi_agent/runtime/langgraph_workflow.py").read_text(
            encoding="utf-8"
        )
        cls.prompts = (ROOT / "bi_agent/runtime/llm_prompts.py").read_text(
            encoding="utf-8"
        )
        cls.agent_core = (ROOT / "bi_agent/conversation/agent_core.py").read_text(
            encoding="utf-8"
        )
        cls.conversation = (ROOT / "bi_agent/conversation/runtime.py").read_text(
            encoding="utf-8"
        )
        cls.baselines = (ROOT / "bi_agent/runtime/baseline_semantics.py").read_text(
            encoding="utf-8"
        )

    def test_intent_node_uses_typed_single_authority_prompt_and_durable_resume(self):
        self.assertIn('"single_authority_intent"', self.prompts)
        self.assertIn("IntentRevision.from_provider_binding", self.workflow)
        self.assertIn("load_accepted_transition", self.workflow)
        self.assertIn("save_intent_revision_transition", self.workflow)
        self.assertIn('request["authority_store"]', self.agent_core)
        self.assertIn('"single_authority_decision_binding"', self.prompts)
        self.assertIn("comparison_spec_contract", self.workflow)
        self.assertIn("scope, time_spec, comparison_spec", self.prompts)
        self.assertIn("Never invent ", self.prompts)
        self.assertIn("an event_ref or physical event window", self.prompts)

    def test_clarification_node_persists_stable_options_before_waiting(self):
        self.assertIn('"single_authority_clarification"', self.prompts)
        self.assertIn("save_decision_options_transition", self.workflow)
        self.assertIn("save_waiting_transition", self.workflow)
        self.assertIn('node_name="persist_waiting_for_decision"', self.workflow)
        self.assertIn('next_transition="await_user_decision"', self.workflow)
        self.assertIn('"decision_ledger_position"', self.workflow)
        self.assertNotIn("_BASELINE_CLARIFICATION_OPTIONS", self.workflow)
        self.assertNotIn("_review_material_clarification_output", self.workflow)

    def test_option_submission_resumes_on_the_same_run_attempt(self):
        route = (ROOT / "app/api/runs/[runId]/clarifications/route.ts").read_text(
            encoding="utf-8"
        )
        store = (ROOT / "bi_agent/conversation/postgres_store.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("attemptRunId: runId", route)
        self.assertIn("sourceRunId: runId", route)
        self.assertIn('producerKind: "clarification_resolution"', route)
        self.assertIn("scopeRef: runId", route)
        self.assertIn('agentCore.status !== "started"', route)
        self.assertIn("loadCustomerAnalysisSnapshot", route)
        self.assertIn("onDetachedWorkerExit", route)
        self.assertNotIn("forceInline", route)
        self.assertNotIn("claimClarificationResolutionAttempt", route)
        self.assertGreater(
            route.index("runAgentCore("),
            route.index("acquireRunDispatchLease({"),
        )
        self.assertIn("store.accept_decision_option", self.agent_core)
        self.assertIn("_resume_authoritative_plan_after_decision", self.agent_core)
        self.assertIn('next_transition="compile_authoritative_plan"', store)
        self.assertNotIn("confirm_business_understanding", self.workflow)
        self.assertNotIn("confirmed_business_understanding", self.workflow)

    def test_duplicate_material_authority_projections_are_removed(self):
        product_sources = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "bi_agent/conversation/agent_core.py",
                "bi_agent/conversation/runtime.py",
                "bi_agent/conversation/postgres_store.py",
                "bi_agent/conversation/store.py",
                "bi_agent/runtime/langgraph_workflow.py",
            )
        )
        for removed in (
            "intent_material",
            "route_material",
            "execution_material",
            "completed_material_authority",
        ):
            self.assertNotIn(removed, product_sources)

    def test_data_prompt_isolation_rejects_request_control_aliases(self):
        product_sources = "\n".join(
            (
                self.workflow,
                self.agent_core,
                (ROOT / "bi_agent/runtime/analysis_runtime.py").read_text(
                    encoding="utf-8"
                ),
            )
        )
        for request_control_alias in (
            "force_failure",
            "force_langgraph_failure",
            "accepted_degradation_choice",
            "run_mode",
        ):
            self.assertNotIn(request_control_alias, product_sources)

    def test_open_business_semantics_have_no_local_keyword_authority(self):
        for function_name in (
            "_classify_intent",
            "_topic_relation",
            "_analysis_objectives",
            "_mentioned_dimensions",
            "_mentioned_metrics",
            "_mentioned_aggregation_scopes",
            "_mentioned_patterns",
            "_looks_new_topic",
            "_needs_clarification",
            "_looks_like_clarification_answer",
        ):
            self.assertNotIn(f"def {function_name}(", self.conversation)

    def test_baseline_contract_does_not_parse_display_labels(self):
        self.assertNotIn("_ALIASES", self.baselines)
        self.assertNotIn("_PREVIOUS_DAY_ALIASES", self.baselines)


if __name__ == "__main__":
    unittest.main()
