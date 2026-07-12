from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pytest

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.models import ClarificationOption, ClarificationState
from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.analysis_contracts import analysis_contract_signature
from bi_agent.runtime.dataset_catalog import DatasetCatalog
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase7.test_conversation_persistence import FakeConnection
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult


def test_early_clarification_resume_preserves_source_topic_family(monkeypatch):
    from bi_agent.runtime import langgraph_workflow as workflow

    original_intent = {
        "question_family": "business_object_impact_review",
        "question_families": [
            "business_object_impact_review",
            "segment_or_factor_attribution",
        ],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": ["segment_or_factor_attribution"],
        "target_metric": "paid_amount",
        "pattern_family": "custom_baseline",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "yesterday",
        "target_claim": "business_object_impact",
        "baseline_candidates": [],
        "sub_intents": [],
        "ambiguous_slots": ["baseline"],
        "answer_contract": {"direct_answer": True},
        "baseline": {},
        "target": {},
        "question": "original topic question",
        "requested_nodes": [],
    }
    monkeypatch.setattr(
        workflow,
        "_invoke_llm",
        lambda state, node, payload: {
            "question_family": "pattern_explanation",
            "question_families": ["pattern_explanation"],
            "target_metric": "paid_amount",
            "pattern_family": "month_start",
            "answer_contract": {"direct_answer": True},
            "analysis_requirements": {
                "context_sources": [],
                "claim_intents": [],
                "requested_dimensions": [],
                "requested_components": [],
            },
        },
    )
    state = {
        "request": {
            "thread_id": "thread-early-source",
            "topic_id": "topic-early-source",
            "question": "original topic question",
            "clarification_choice": {"answer_text": "selected baseline"},
                "clarification_resume_context": {
                "resume_run_id": "run-early-source",
                "source_thread_id": "thread-early-source",
                "source_topic_id": "topic-early-source",
                "question": "original topic question",
                    "original_intent": original_intent,
                        "material_slots": {
                            "target_metrics": ["paid_amount"],
                            "baselines": [],
                            "scope": "full_sample",
                        },
                },
        }
    }

    workflow._understand_business_intent(state)

    assert state["intent"]["question_family"] == "business_object_impact_review"
    assert state["intent"]["question_families"] == [
        "business_object_impact_review",
        "segment_or_factor_attribution",
    ]
    assert state["intent"]["question"] == "original topic question"


def test_query_gap_clarification_persists_original_topic_material(tmp_path):
    from types import SimpleNamespace
    from bi_agent.runtime import langgraph_workflow as workflow

    source_contract = _source_contract("run-query-gap-source")
    intent = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "context_sources": ["external_event"],
        "claim_intents": ["candidate_mechanism"],
        "requested_dimensions": ["channel"],
        "requested_components": [],
        "question": "arbitrary source topic",
    }
    state = {
        "run_id": "run-query-gap-source",
        "request": {"artifact_root": str(tmp_path)},
        "intent": intent,
        "analysis_route": {
            "requested_nodes": ["event_evidence"],
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "context_sources": ["external_event"],
                "claim_intents": ["candidate_mechanism"],
                "requested_dimensions": ["channel"],
            },
        },
        "compiled_graph": SimpleNamespace(
            mutations=SimpleNamespace(accepted_graph=("event_evidence",))
        ),
        "analysis_runtime_result": SimpleNamespace(
            analysis_contract=SimpleNamespace(to_dict=lambda: source_contract),
            query_contracts=(),
        ),
        "query_gap_clarification": {"questions": []},
        "query_repair_decisions": [],
        "staged_query_gap_actions": [],
    }

    workflow._persist_query_gap_clarification(state)

    assert state["answer_package"]["original_intent"] == intent
    assert state["answer_package"]["material_slots"] == {
        "target_metrics": ["paid_amount"],
        "context_sources": ["external_event"],
        "claim_intents": ["candidate_mechanism"],
        "requested_dimensions": ["channel"],
    }


def test_query_gap_resume_context_roundtrips_source_run_topic_and_material():
    store = InMemoryConversationStore()
    store.create_thread("thread-query-gap-roundtrip", owner_id="analyst")
    topic = store.create_topic(
        "thread-query-gap-roundtrip",
        title="source topic",
        summary="source topic",
    )
    store.set_current_topic("thread-query-gap-roundtrip", topic.topic_id)
    original_intent = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "context_sources": ["gameplay"],
        "claim_intents": ["candidate_mechanism"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source business question",
    }
    material_slots = {
        "target_metrics": ["paid_amount"],
        "context_sources": ["gameplay"],
        "claim_intents": ["candidate_mechanism"],
    }
    source_run_id = "run-query-gap-roundtrip"
    store.upsert_run(
        source_run_id,
        thread_id="thread-query-gap-roundtrip",
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "thread_id": "thread-query-gap-roundtrip",
            "topic_id": topic.topic_id,
            "question": "source business question",
            "original_intent": original_intent,
            "material_slots": material_slots,
            "clarification": {
                "questions": [{
                    "question": "choose",
                    "options": ["continue source topic"],
                }],
            },
        },
    )
    store.set_pending_clarification(
        "thread-query-gap-roundtrip", topic.topic_id, source_run_id
    )
    store.save_clarification_state(
        ClarificationState(
            run_id=source_run_id,
            topic_id=topic.topic_id,
            question="choose",
            options=[
                ClarificationOption(
                    option_id="continue",
                    label="continue source topic",
                    description="continue source topic",
                )
            ],
        )
    )

    result = ConversationRuntime(store).handle_message(
        "thread-query-gap-roundtrip", "continue source topic"
    )

    resume = result.run_request.clarification_resume_context
    assert resume["resume_run_id"] == source_run_id
    assert resume["source_thread_id"] == "thread-query-gap-roundtrip"
    assert resume["source_topic_id"] == topic.topic_id
    assert resume["question"] == "source business question"
    assert resume["original_intent"] == original_intent
    assert resume["material_slots"] == material_slots


@pytest.mark.parametrize(
    "corruption",
    [
        "thread",
        "topic",
        "material",
        "persisted_material",
        "context_conflict",
        "target_conflict",
        "component_conflict",
        "claim_extra_unauthorized",
    ],
)
def test_resume_intent_authority_rejects_owner_or_material_corruption(corruption):
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    original = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "context_sources": ["gameplay"],
        "claim_intents": ["candidate_mechanism"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    if corruption == "material":
        original["context_sources"] = ["paid_order_success"]
    resume = {
        "resume_run_id": "run-source",
        "source_thread_id": "thread-source",
        "source_topic_id": "topic-source",
        "question": "source question",
        "original_intent": original,
        "material_slots": {"target_metrics": ["paid_amount"]},
    }
    request = {
        "thread_id": "thread-source",
        "topic_id": "topic-source",
        "question": "source question",
        "clarification_resume_context": resume,
    }
    if corruption == "thread":
        resume["source_thread_id"] = "thread-other"
    elif corruption == "topic":
        resume["source_topic_id"] = "topic-other"
    elif corruption == "persisted_material":
        resume["material_slots"] = {"context_sources": ["paid_order_success"]}
    elif corruption == "context_conflict":
        resume["material_slots"] = {
            "target_metrics": ["paid_amount"],
            "context_sources": ["external_event"],
        }
    elif corruption == "target_conflict":
        resume["material_slots"] = {"target_metrics": ["paid_users"]}
    elif corruption == "component_conflict":
        resume["material_slots"] = {
            "target_metrics": ["paid_amount"],
            "requested_components": ["paid_users"],
        }
    elif corruption == "claim_extra_unauthorized":
        resume["material_slots"] = {
            "target_metrics": ["paid_amount"],
            "claim_intents": ["contract_coverage_and_trust_boundary"],
        }

    with pytest.raises(workflow.WorkflowFailure):
        workflow._bind_clarification_resume_intent(
            {"question_family": "pattern_explanation"}, request, registry
        )


def test_resume_allows_source_contract_authorized_trust_boundary_claim():
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    original = {
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    source_contract = _source_contract()
    source_contract.pop("contract_signature", None)
    source_contract["claim_intents"] = [
        "comparative_change",
        "contract_coverage_and_trust_boundary",
    ]
    resume = {
        "resume_run_id": "run-source",
        "source_thread_id": "thread-source",
        "source_topic_id": "topic-source",
        "question": "source question",
        "original_intent": original,
        "material_slots": {
            "target_metrics": ["paid_amount"],
            "claim_intents": [
                "comparative_change",
                "contract_coverage_and_trust_boundary",
            ],
        },
        "analysis_contract": source_contract,
    }
    bound = workflow._bind_clarification_resume_intent(
        {},
        {
            "thread_id": "thread-source",
            "topic_id": "topic-source",
            "question": "source question",
            "clarification_resume_context": resume,
        },
        registry,
    )
    assert bound["claim_intents"] == ["comparative_change"]


def _source_contract(run_id="run-source"):
    outcome = compile_analysis_contract(
        run_id=run_id,
        proposal={"target_metrics": ["paid_amount"]},
        accepted_capabilities=("compare_periods", "answer_verify"),
        catalog=DatasetCatalog(()),
        registry=RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    payload = outcome.analysis_contract.to_dict()
    payload["contract_signature"] = analysis_contract_signature(payload)
    return payload


def _choice(run_id="run-source"):
    return {
        "choice_id": "continue-boundary",
        "action_kind": "continue_with_boundary_only",
        "business_label": "保留边界并继续",
        "affected_capabilities": ["compare_periods"],
        "source_run_id": run_id,
    }


def _segment_closure_contract(run_id="run-segment-source"):
    from bi_agent.runtime.analysis_contracts import ContractGap

    outcome = compile_analysis_contract(
        run_id=run_id,
        proposal={
            "question_families": ["segment_or_factor_attribution"],
            "target_metrics": ["paid_amount"],
            "requested_dimensions": ["channel"],
            "baselines": ["previous_day"],
            "claim_intents": ["segment_contribution_or_mix_shift"],
        },
        accepted_capabilities=(
            "data_quality_profile",
            "answer_verify",
            "gameplay_activity_context",
            "segment_breakdown",
            "segment_shift_compare",
        ),
        catalog=DatasetCatalog(()),
        registry=RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    contract = replace(
        outcome.analysis_contract,
        contract_gaps=(
            ContractGap(
                gap_type="source_unbound",
                gap_id="dataset:gameplay:source_unbound",
                dataset_id="gameplay",
                affected_capabilities=("gameplay_activity_context",),
                owner="data_owner",
                repair_options=("bind_source",),
                requires_clarification=True,
            ),
            ContractGap(
                gap_type="permission_blocked",
                gap_id="dataset:gameplay_channel:permission_blocked",
                dataset_id="gameplay_channel",
                affected_capabilities=(
                    "segment_breakdown",
                    "segment_shift_compare",
                ),
                owner="permission_owner",
                repair_options=("request_permission",),
                requires_clarification=True,
            ),
        ),
    ).to_dict()
    contract["contract_signature"] = analysis_contract_signature(contract)
    return contract


def _seed_memory_store():
    store = InMemoryConversationStore()
    store.upsert_run(
        "run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        status="waiting_for_clarification",
        request={"analysis_contract": {"tampered": "request-context-only"}},
    )
    contract = _source_contract()
    store.analysis_runtime_authority["analysis_contract"][
        contract["analysis_contract_id"]
    ] = contract
    return store, contract


def test_memory_resume_authority_ignores_mutable_request_and_binds_actual_choice():
    store, contract = _seed_memory_store()
    outcome_ref = store.record_clarification_outcome(
        source_run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        choice=_choice(),
    )

    authority = store.resolve_clarification_resume_authority(
        source_run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        choice=_choice(),
        outcome_ref=outcome_ref,
    )

    assert authority["analysis_contract"] == {
        key: value for key, value in contract.items() if key != "contract_signature"
    }
    assert authority["analysis_contract_signature"] == contract["contract_signature"]
    assert authority["clarification_outcome"]["outcome_ref"] == outcome_ref
    assert authority["clarification_outcome"]["choice"] == _choice()


def test_clarification_outcome_cannot_be_persisted_under_a_different_owner():
    store, _ = _seed_memory_store()

    with pytest.raises(
        EvidenceIntegrityError, match="clarification_outcome_owner_mismatch"
    ):
        store.record_clarification_outcome(
            source_run_id="run-source",
            thread_id="thread-other",
            topic_id="topic-1",
            choice=_choice(),
        )

    assert not any(
        event["event_type"] == "clarification_outcome_recorded"
        for event in store.audit_events
    )


def test_clarification_outcome_cannot_be_persisted_for_a_stale_source_run():
    store, _ = _seed_memory_store()
    store.runs["run-source"]["status"] = "completed"

    with pytest.raises(
        EvidenceIntegrityError, match="clarification_outcome_source_run_stale"
    ):
        store.record_clarification_outcome(
            source_run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            choice=_choice(),
        )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("stale", "clarification_resume_source_run_missing"),
        ("stale_status", "clarification_resume_source_run_stale"),
        ("owner", "clarification_resume_owner_mismatch"),
        ("contract_signature", "clarification_resume_contract_signature_invalid"),
        ("outcome_signature", "clarification_resume_outcome_signature_invalid"),
        ("choice", "clarification_resume_choice_mismatch"),
    ],
)
def test_memory_resume_authority_fails_closed_on_stale_tampered_or_owner_drift(
    mutation, reason
):
    store, contract = _seed_memory_store()
    outcome_ref = store.record_clarification_outcome(
        source_run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        choice=_choice(),
    )
    source_run_id = "run-missing" if mutation == "stale" else "run-source"
    thread_id = "thread-other" if mutation == "owner" else "thread-1"
    if mutation == "contract_signature":
        contract["contract_signature"] = "sha256:tampered"
    if mutation == "stale_status":
        store.runs["run-source"]["status"] = "completed"
    outcome = next(
        event for event in store._audit_events
        if event["event_type"] == "clarification_outcome_recorded"
    )["payload"]
    if mutation == "outcome_signature":
        outcome["outcome_signature"] = "sha256:tampered"
    if mutation == "choice":
        outcome["choice"] = {**outcome["choice"], "choice_id": "tampered"}
        from bi_agent.runtime.analysis_contracts import stable_contract_signature
        body = {key: value for key, value in outcome.items() if key != "outcome_signature"}
        outcome["outcome_signature"] = stable_contract_signature(body)

    with pytest.raises(EvidenceIntegrityError, match=reason):
        store.resolve_clarification_resume_authority(
            source_run_id=source_run_id,
            thread_id=thread_id,
            topic_id="topic-1",
            choice=_choice(),
            outcome_ref=outcome_ref,
        )


def test_postgres_resume_authority_selects_immutable_contract_and_outcome_with_owner():
    contract = _source_contract()
    choice = _choice()
    from bi_agent.runtime.analysis_contracts import stable_contract_signature
    outcome_body = {
        "source_run_id": "run-source",
        "thread_id": "thread-1",
        "topic_id": "topic-1",
        "choice": choice,
    }
    outcome_ref = "clarification-outcome:" + stable_contract_signature(outcome_body)
    outcome = {"outcome_ref": outcome_ref, **outcome_body}
    outcome["outcome_signature"] = stable_contract_signature(outcome)
    connection = FakeConnection(rows=[{
        "analysis_contract_id": contract["analysis_contract_id"],
        "analysis_run_id": "run-source",
        "stored_contract_signature": contract["contract_signature"],
        "contract_payload": json.dumps(
            {key: value for key, value in contract.items() if key != "contract_signature"}
        ),
        "run_status": "waiting_for_clarification",
        "run_thread_id": "thread-1",
        "run_topic_id": "topic-1",
        "outcome_payload": json.dumps(outcome),
        "outcome_ref": outcome_ref,
        "outcome_run_id": "run-source",
        "outcome_thread_id": "thread-1",
        "outcome_topic_id": "topic-1",
    }])

    resolved = PostgresConversationStore(connection).resolve_clarification_resume_authority(
        source_run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        choice=choice,
        outcome_ref=outcome_ref,
    )

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "waje_runtime.analysis_contracts" in sql
    assert "waje_runtime.audit_events" in sql
    assert "waje_runtime.analysis_runs" in sql
    assert "request" not in sql.lower()
    assert resolved["clarification_outcome"]["outcome_ref"] == outcome_ref


def test_postgres_outcome_record_locks_and_requires_waiting_source_run():
    connection = FakeConnection(rows=[{
        "thread_id": "thread-1",
        "topic_id": "topic-1",
        "status": "waiting_for_clarification",
    }])

    outcome_ref = PostgresConversationStore(connection).record_clarification_outcome(
        source_run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        choice=_choice(),
    )

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "FOR UPDATE" in sql
    assert "status" in sql
    assert outcome_ref.startswith("clarification-outcome:")


def test_agent_core_resume_injects_authority_resolved_from_persisted_source_bundle():
    from tests.phase7.test_analysis_runtime_persistence import _authority_bundle

    calls = []

    def workflow(request):
        calls.append(dict(request))
        if len(calls) == 1:
            records = _authority_bundle(
                run_id=request["run_id"],
                thread_id=request["thread_id"],
                topic_id=request["topic_id"],
                analysis_contract_ref=f"analysis:{request['run_id']}:1",
            )
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["segment_contribution"],
                    "analysis_contract": records["analysis_contract"],
                    "analysis_route": {
                        "requested_nodes": ["segment_contribution"]
                    },
                    "clarification": {
                        "questions": [{
                            "question": "当前来源缺口怎么处理？",
                            "options": ["保留证据边界继续", "等待来源"],
                        }],
                        "recommended_assumption": {
                            "option": "保留证据边界继续"
                        },
                        "choice_actions": [{
                            "choice_id": "boundary",
                            "action_kind": "continue_with_boundary_only",
                            "business_label": "保留证据边界继续",
                            "affected_capabilities": ["segment_contribution"],
                        }],
                    },
                },
                analysis_runtime_records=records,
            )
        return WorkflowRunResult(
            status="failed",
            run_id=request["run_id"],
            failure_reason="test_stop_after_authority_resolution",
        )

    core = ConversationAgentCore(InMemoryConversationStore(), workflow_runner=workflow)
    first = core.run_message(
        thread_id="thread-authority-e2e",
        run_id="run-authority-source",
        user_message="昨天渠道表现如何？",
    )
    resumed = core.run_message(
        thread_id="thread-authority-e2e",
        run_id="run-authority-resumed",
        user_message="按推荐继续",
        clarification={"answer_text": "按推荐继续"},
    )

    assert first["status"] == "waiting_for_clarification"
    assert resumed["status"] == "failed"
    authority = calls[1]["accepted_terminal_gap_authority"]
    assert authority["source_run_id"] == "run-authority-source"
    assert authority["analysis_contract"]["analysis_contract_id"] == (
        "analysis:run-authority-source:1"
    )
    assert authority["clarification_outcome"]["choice"] == (
        calls[1]["accepted_degradation_choice"]
    )
    assert calls[1]["clarification_outcome_ref"].startswith(
        "clarification-outcome:"
    )


def test_agent_core_resume_closes_every_nonready_obligation_from_authority():
    store = InMemoryConversationStore()
    store.create_thread("thread-segment-closure")
    topic = store.create_topic(
        "thread-segment-closure", title="渠道贡献", summary="渠道贡献分析"
    )
    store.set_current_topic("thread-segment-closure", topic.topic_id)
    contract = _segment_closure_contract()
    choice_action = {
        "choice_id": "continue-segment-boundary",
        "action_kind": "continue_with_boundary_only",
        "business_label": "保留边界继续",
        "affected_capabilities": ["gameplay_activity_context"],
    }
    store.upsert_run(
        "run-segment-source",
        thread_id="thread-segment-closure",
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "question": "昨天渠道贡献如何？",
            "accepted_graph": list(contract["capability_requirements"]),
            "analysis_contract": contract,
            "clarification": {
                "questions": [{
                    "question": "缺口怎么处理？",
                    "options": ["保留边界继续", "等待来源"],
                }],
                "recommended_assumption": {"option": "保留边界继续"},
                "choice_actions": [choice_action],
            },
        },
    )
    store.analysis_runtime_authority["analysis_contract"][
        contract["analysis_contract_id"]
    ] = contract
    store.set_pending_clarification(
        "thread-segment-closure", topic.topic_id, "run-segment-source"
    )
    store.save_clarification_state(ClarificationState(
        run_id="run-segment-source",
        topic_id=topic.topic_id,
        question="缺口怎么处理？",
        options=[ClarificationOption(
            option_id="continue-segment-boundary",
            label="保留边界继续",
            description="保留边界继续",
            recommended=True,
        )],
    ))
    calls = []

    def workflow(request):
        calls.append(dict(request))
        return WorkflowRunResult(
            status="failed",
            run_id=request["run_id"],
            failure_reason="test_stop_after_authority_resolution",
        )

    result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
        thread_id="thread-segment-closure",
        run_id="run-segment-resumed",
        user_message="按推荐继续",
        clarification={"answer_text": "按推荐继续"},
    )

    assert result["status"] == "failed"
    effective = calls[0]["accepted_degradation_choice"]
    assert effective["affected_capabilities"] == [
        "gameplay_activity_context",
        "segment_breakdown",
        "segment_shift_compare",
    ]
    assert "data_quality_profile" not in effective["affected_capabilities"]
    assert "answer_verify" not in effective["affected_capabilities"]
    assert calls[0]["accepted_terminal_gap_authority"][
        "clarification_outcome"
    ]["choice"] == effective
    from bi_agent.runtime.analysis_contract_compiler import (
        _accepted_terminal_gap_authority,
    )

    carried, outcome_ref = _accepted_terminal_gap_authority({
        "accepted_degradation_choice": effective,
        "accepted_terminal_gap_authority": calls[0][
            "accepted_terminal_gap_authority"
        ],
        "resume_thread_id": "thread-segment-closure",
        "resume_topic_id": topic.topic_id,
    })
    assert outcome_ref == calls[0]["clarification_outcome_ref"]
    assert [gap.gap_id for gap in carried] == [
        "dataset:gameplay:source_unbound",
        "dataset:gameplay_channel:permission_blocked",
    ]
    assert {
        capability
        for gap in carried
        for capability in gap.affected_capabilities
    } >= {
        "gameplay_activity_context",
        "segment_breakdown",
        "segment_shift_compare",
    }


def test_agent_core_returns_typed_failure_when_resume_authority_rejects():
    store = InMemoryConversationStore()
    store.create_thread("thread-resume-reject")
    topic = store.create_topic(
        "thread-resume-reject", title="付费分析", summary="昨天付费"
    )
    store.set_current_topic("thread-resume-reject", topic.topic_id)
    store.upsert_run(
        "run-resume-reject-source",
        thread_id="thread-resume-reject",
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "question": "昨天付费如何？",
            "clarification": {
                "questions": [{
                    "question": "怎么继续？",
                    "options": ["保留边界继续"],
                }],
                "recommended_assumption": {"option": "保留边界继续"},
                "choice_actions": [{
                    "choice_id": "boundary",
                    "action_kind": "continue_with_boundary_only",
                    "business_label": "保留边界继续",
                    "affected_capabilities": ["answer_verify"],
                }],
            },
        },
    )
    store.set_pending_clarification(
        "thread-resume-reject", topic.topic_id, "run-resume-reject-source"
    )
    store.save_clarification_state(ClarificationState(
        run_id="run-resume-reject-source",
        topic_id=topic.topic_id,
        question="怎么继续？",
        options=[ClarificationOption(
            option_id="boundary",
            label="保留边界继续",
            description="保留边界继续",
            recommended=True,
        )],
    ))

    def reject_authority(**_):
        raise EvidenceIntegrityError("clarification_resume_contract_signature_invalid")

    store.resolve_clarification_resume_authority = reject_authority
    core = ConversationAgentCore(
        store,
        workflow_runner=lambda _: pytest.fail("workflow must not run"),
    )

    result = core.run_message(
        thread_id="thread-resume-reject",
        run_id="run-resume-reject-current",
        user_message="按推荐继续",
        clarification={"answer_text": "按推荐继续"},
    )

    assert result["status"] == "failed"
    assert result["failure_reason"] == "clarification_resume_authority_failed"
    assert store.runs["run-resume-reject-current"]["status"] == "failed"
    assert any(
        event["event_type"] == "clarification_resume_authority_failed"
        for event in store.audit_events
    )
