from __future__ import annotations

from types import SimpleNamespace

import pytest

from bi_agent.conversation.models import CLARIFICATION_ESCAPE_OPTION
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.single_authority import DurableTransition, LifecycleState


def _decision_options() -> list[dict[str, object]]:
    return [
        {
            "slot_id": "comparison_baseline",
            "option_id": "comparison_baseline.previous_day",
            "typed_value": {"baseline_id": "previous_day"},
            "display_label": "跟前一天比较（推荐）",
            "display_description": "用于观察相邻日期变化。",
            "recommended": True,
        },
        {
            "slot_id": "comparison_baseline",
            "option_id": "comparison_baseline.same_weekday_last_week",
            "typed_value": {"baseline_id": "same_weekday_last_week"},
            "display_label": "跟上周同日比较",
            "display_description": "用于观察同星期位置的变化。",
            "recommended": False,
        },
    ]


class _DecisionOptionReader(PostgresConversationStore):
    def __init__(self, rows) -> None:
        self.rows = rows

    def _fetchall(self, _sql, _params):
        return self.rows


def _signed_decision_option_rows():
    intent_revision_id = "intent-clarification-reader"
    options = _decision_options()
    option_set_digest = canonical_digest(options)
    rows = []
    for display_position, option in enumerate(options, start=1):
        body = canonical_value(
            {
                "intent_revision_id": intent_revision_id,
                **option,
                "option_set_digest": option_set_digest,
            }
        )
        content_digest = canonical_digest(body)
        rows.append(
            (
                intent_revision_id,
                option["slot_id"],
                option["option_id"],
                option["typed_value"],
                option["display_label"],
                option["display_description"],
                option["recommended"],
                display_position,
                option_set_digest,
                content_digest,
                {**body, "content_digest": content_digest},
            )
        )
    return rows


def test_decision_option_reader_validates_signed_records_and_set_digest():
    rows = _signed_decision_option_rows()

    loaded = _DecisionOptionReader(rows).load_decision_options(
        "intent-clarification-reader"
    )

    assert loaded == tuple(_decision_options())
    tampered = list(rows)
    tampered[0] = (*tampered[0][:-1], {**tampered[0][-1], "recommended": False})
    with pytest.raises(EvidenceIntegrityError, match="decision_option_record_invalid"):
        _DecisionOptionReader(tampered).load_decision_options(
            "intent-clarification-reader"
        )


class _StageSealReader:
    def __init__(self, attempt_ref: str | None) -> None:
        self.attempt_ref = attempt_ref

    def load_stage_attempt_refs(self, **_kwargs):
        return (self.attempt_ref,) if self.attempt_ref else ()


class _ClarificationReader(PostgresConversationStore):
    def __init__(self, *, request_clarification=None, stage_attempt_ref="attempt-1"):
        self.run_id = "run-clarification-reader"
        self.thread_id = "thread-clarification-reader"
        self.topic_id = "topic-clarification-reader"
        self.intent_revision_id = "intent-clarification-reader"
        self.slot = {
            "slot_id": "comparison_baseline",
            "slot_kind": "baseline",
            "materiality": "material",
            "status": "unresolved",
            "question": "需要按哪个基线比较？",
            "allowed_value_refs": ("previous_day", "same_weekday_last_week"),
        }
        self.intent = SimpleNamespace(
            intent_revision_id=self.intent_revision_id,
            ambiguity_slots=(self.slot,),
            time_spec={"kind": "date", "target": "2026-06-19"},
        )
        self.options = _decision_options()
        self.outcome = {
            "status": "question_tool_opened",
            "boundary_status": "needs_question",
            "questions": [
                {
                    "slot_id": "comparison_baseline",
                    "slot_kind": "baseline",
                    "question": "本次付费金额变化需要按哪个业务基线比较？",
                    "options": [
                        {
                            "option_id": option["option_id"],
                            "label": option["display_label"],
                            "description": option["display_description"],
                            "recommended": option["recommended"],
                            "typed_value": option["typed_value"],
                        }
                        for option in self.options
                    ]
                    + [
                        {
                            "option_id": "tell_agent_differently",
                            "label": CLARIFICATION_ESCAPE_OPTION,
                            "description": "自己说明当前业务选择，或明确修改分析目标。",
                            "recommended": False,
                        }
                    ],
                    "recommendation_reason": "相邻日期最贴近当前业务问题。",
                }
            ],
            "status_message": "等待确认比较基线。",
        }
        self.generate_input = {
            "intent_revision_ref": self.intent_revision_id,
            "clarification_slots": [{"slot": self.slot}],
        }
        self.generate_output = {
            "decision_options": self.options,
            "clarification_outcome": self.outcome,
            "raw_provider_output": {},
        }
        self.generate_transition = DurableTransition.create(
            node_name="generate_clarification",
            parent_transition_id="transition-bind-intent",
            run_attempt_id=self.run_id,
            intent_revision_id=self.intent_revision_id,
            decision_ledger_position=0,
            input_digest=canonical_digest(self.generate_input),
            output_digest=canonical_digest(self.generate_output),
            execution_attempt=1,
            provider_ref="provider-test",
            model_ref="model-test",
            status="succeeded",
            acceptance_state="accepted",
            next_transition="persist_waiting_for_decision",
        )
        self.lifecycle = LifecycleState.create(
            run_attempt_id=self.run_id,
            execution_state="waiting",
            interaction_state="waiting_for_user",
        )
        self.waiting_input = {
            "intent_revision_id": self.intent_revision_id,
            "decision_ledger_position": 0,
            "decision_options_digest": canonical_digest(self.options),
            "clarification_digest": canonical_digest(self.outcome),
            "parent_transition_id": self.generate_transition.transition_id,
        }
        self.waiting_output = {
            "status": "waiting_for_clarification",
            "lifecycle_state": self.lifecycle.to_dict(),
        }
        self.waiting_transition = DurableTransition.create(
            node_name="persist_waiting_for_decision",
            parent_transition_id=self.generate_transition.transition_id,
            run_attempt_id=self.run_id,
            intent_revision_id=self.intent_revision_id,
            decision_ledger_position=0,
            input_digest=canonical_digest(self.waiting_input),
            output_digest=canonical_digest(self.waiting_output),
            execution_attempt=1,
            provider_ref="local_deterministic",
            model_ref="contract_policy",
            status="succeeded",
            acceptance_state="accepted",
            next_transition="await_user_decision",
        )
        self.request = {
            "schema_version": "single-authority-phase02-waiting.v1",
            "run_attempt_id": self.run_id,
            "thread_id": self.thread_id,
            "turn_id": "turn-clarification-reader",
            "topic_id": self.topic_id,
            "turn_intent": "new_topic",
            "topic_relation": "new_topic",
            "intent_revision_id": self.intent_revision_id,
            "decision_ledger_position": 0,
            "accepted_transition_id": self.waiting_transition.transition_id,
            "clarification": (
                self.outcome if request_clarification is None else request_clarification
            ),
            "context_manifest_ref": "context-manifest-reader",
            "runtime_descriptors": {},
        }
        self.attempt_journal = _StageSealReader(stage_attempt_ref)

    def get_thread(self, _thread_id):
        return SimpleNamespace(
            pending_clarification_id=self.run_id,
            pending_clarification_topic_id=self.topic_id,
        )

    def get_run_state(self, _run_id):
        return {
            "thread_id": self.thread_id,
            "topic_id": self.topic_id,
            "status": "waiting_for_clarification",
            "request": self.request,
        }

    def resolve_active_intent_revision(self, _run_id):
        return self.intent

    def _fetchall(self, sql, _params):
        assert "generate_clarification" in sql
        return [(self.generate_transition.input_digest,)]

    def _fetchone(self, sql, _params):
        assert "persist_waiting_for_decision" in sql
        return [(self.waiting_transition.input_digest,)][0]

    def load_accepted_transition(self, *, node_name, input_digest, **_kwargs):
        if node_name == "generate_clarification":
            assert input_digest == self.generate_transition.input_digest
            return {
                "transition": self.generate_transition,
                "input_payload": self.generate_input,
                "output_payload": self.generate_output,
            }
        assert node_name == "persist_waiting_for_decision"
        assert input_digest == self.waiting_transition.input_digest
        return {
            "transition": self.waiting_transition,
            "input_payload": self.waiting_input,
            "output_payload": self.waiting_output,
        }

    def load_decision_options(self, _intent_revision_id):
        return tuple(self.options)

    def load_decision_ledger(self, _intent_revision_id):
        return SimpleNamespace(active_for_slot=lambda _slot_id: None)

    def latest_accepted_transition_id(self, _run_id):
        return self.waiting_transition.transition_id

    def latest_lifecycle_state(self, _run_id):
        return self.lifecycle


def test_open_clarification_reads_only_closed_single_authority_records():
    store = _ClarificationReader()

    state = store.get_open_clarification(store.thread_id)

    assert state is not None
    assert state.question == store.outcome["questions"][0]["question"]
    assert [option.option_id for option in state.options] == [
        "comparison_baseline.previous_day",
        "comparison_baseline.same_weekday_last_week",
        "tell_agent_differently",
    ]


def test_open_clarification_rejects_unsealed_or_divergent_projection():
    with pytest.raises(
        EvidenceIntegrityError,
        match="pending_clarification_transition_invalid",
    ):
        _ClarificationReader(stage_attempt_ref=None).get_open_clarification(
            "thread-clarification-reader"
        )

    with pytest.raises(
        EvidenceIntegrityError,
        match="pending_clarification_waiting_closure_invalid",
    ):
        _ClarificationReader(request_clarification={}).get_open_clarification(
            "thread-clarification-reader"
        )

    legacy_alias = _ClarificationReader()
    legacy_alias.outcome["recommended_choice_id"] = "comparison_baseline.previous_day"
    with pytest.raises(
        EvidenceIntegrityError,
        match="pending_clarification_projection_invalid",
    ):
        legacy_alias.get_open_clarification("thread-clarification-reader")
