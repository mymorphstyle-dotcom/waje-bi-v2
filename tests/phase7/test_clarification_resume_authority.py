from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pytest

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.clarification_authority import build_clarification_outcome
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
                            "requested_components": [],
                            "requested_dimensions": [],
                            "baselines": [],
                            "context_sources": [],
                            "claim_intents": [],
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
        "requested_components": [],
        "requested_dimensions": ["channel"],
        "baselines": [],
        "context_sources": ["external_event"],
        "claim_intents": ["candidate_mechanism"],
    }


def test_query_gap_material_slots_preserve_explicit_empty_route_axes():
    from bi_agent.runtime import langgraph_workflow as workflow

    state = {
        "intent": {
            "target_metric": "paid_amount",
            "baseline_candidates": ["previous_day"],
            "context_sources": ["external_event"],
            "claim_intents": ["candidate_mechanism"],
            "requested_dimensions": ["channel"],
            "requested_components": ["paid_users"],
        },
        "analysis_route": {
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "requested_components": [],
                "requested_dimensions": [],
                "baselines": [],
                "context_sources": [],
                "claim_intents": [],
                "diagnostic_tags": [],
            }
        },
    }

    assert workflow._clarification_material_slots(state) == {
        "target_metrics": ["paid_amount"],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": [],
        "claim_intents": [],
        "diagnostic_tags": [],
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
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": ["gameplay"],
        "claim_intents": ["candidate_mechanism"],
    }
    source_run_id = "run-query-gap-roundtrip"
    source_contract = _source_contract_with_unsupported_claim(source_run_id)
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
            "analysis_contract": source_contract,
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
    assert resume["analysis_contract"] == source_contract
    assert resume["analysis_contract"]["contract_gaps"][-1][
        "affected_claim_types"
    ] == ("baseline_stability",)


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
        "material_slots": {
            "target_metrics": ["paid_amount"],
            "requested_components": [],
            "requested_dimensions": [],
            "baselines": [],
            "context_sources": ["gameplay"],
            "claim_intents": ["candidate_mechanism"],
        },
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
        resume["material_slots"]["context_sources"] = ["paid_order_success"]
    elif corruption == "context_conflict":
        resume["material_slots"]["context_sources"] = ["external_event"]
    elif corruption == "target_conflict":
        resume["material_slots"]["target_metrics"] = [
            "paid_amount",
            "paid_users",
        ]
    elif corruption == "component_conflict":
        resume["material_slots"]["requested_components"] = ["paid_users"]
    elif corruption == "claim_extra_unauthorized":
        resume["material_slots"]["claim_intents"] = [
            "contract_coverage_and_trust_boundary"
        ]

    expected_axis = (
        "target_metrics" if corruption == "target_conflict" else None
    )
    with pytest.raises(workflow.WorkflowFailure) as raised:
        workflow._bind_clarification_resume_intent(
            {"question_family": "pattern_explanation"}, request, registry
        )
    if expected_axis:
        assert expected_axis in str(raised.value)


@pytest.mark.parametrize(
    "axis,original_update,material_update",
    [
        (
            "target_metrics",
            {"target_metric": "active_users"},
            {"target_metrics": ["active_users"]},
        ),
        (
            "requested_components",
            {"requested_components": ["active_users"]},
            {"requested_components": ["active_users"]},
        ),
        (
            "requested_dimensions",
            {"requested_dimensions": ["channel"]},
            {"requested_dimensions": ["channel"]},
        ),
        (
            "baselines",
            {"baseline_candidates": ["previous_day"]},
            {"baselines": ["previous_day"]},
        ),
        (
            "context_sources",
            {"context_sources": ["external_event"]},
            {"context_sources": ["external_event"]},
        ),
        (
            "claim_intents",
            {"claim_intents": ["observed_activity"]},
            {"claim_intents": ["observed_activity"]},
        ),
        (
            "scope",
            {"scope": "day"},
            {"scope": "day"},
        ),
    ],
)
def test_resume_exact_material_collusion_cannot_override_immutable_contract(
    axis, original_update, material_update
):
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    contract["question_families"] = ["business_object_impact_review"]
    contract["contract_signature"] = analysis_contract_signature(contract)
    authority_original = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "baseline_candidates": [],
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "scope": "full_sample",
        "question": "source question",
    }
    original = {
        **authority_original,
        **original_update,
    }
    authority_material = {
        **_complete_material_slots(
            claim_intents=["comparative_change"]
        ),
        "scope": "full_sample",
    }
    material = {
        **authority_material,
        **material_update,
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_conflict:{axis}",
    ) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                original,
                material,
                analysis_contract=_typed_contract_payload(contract),
                authority_contract=contract,
                authority_original_intent=authority_original,
                authority_material_slots=authority_material,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


def test_resume_exact_material_rejects_original_family_drift_from_authority():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    contract["question_families"] = ["business_object_impact_review"]
    contract["contract_signature"] = analysis_contract_signature(contract)
    original = {
        "question_family": "pattern_explanation",
        "question_families": ["pattern_explanation"],
        "primary_question_family": "pattern_explanation",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    authority_original = {
        **original,
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:question_families",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                original,
                _complete_material_slots(
                    claim_intents=["comparative_change"]
                ),
                analysis_contract=_typed_contract_payload(contract),
                authority_contract=contract,
                authority_original_intent=authority_original,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_resume_exact_material_rejects_mutable_prior_contract_family_drift():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    contract["question_families"] = ["business_object_impact_review"]
    contract["contract_signature"] = analysis_contract_signature(contract)
    mutable_contract = _typed_contract_payload(contract)
    mutable_contract["question_families"] = ["pattern_explanation"]
    original = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:prior_contract",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                original,
                _complete_material_slots(),
                analysis_contract=mutable_contract,
                authority_contract=contract,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_resume_rejects_original_primary_target_drift_to_secondary_authority_target():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract_with_target_metrics(
        ("paid_amount", "paid_users")
    )

    source_original = {
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    source_material = _complete_material_slots(
        target_metrics=["paid_amount", "paid_users"],
        claim_intents=["comparative_change"],
    )
    request = _resume_request(
        source_original,
        source_material,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )
    request["clarification_resume_context"]["original_intent"][
        "target_metric"
    ] = "paid_users"

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:target_metrics",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            request,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_resume_rejects_internally_conflicting_original_family_copies():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    contract["question_families"] = [
        "business_object_impact_review",
        "segment_or_factor_attribution",
    ]
    contract["contract_signature"] = analysis_contract_signature(contract)
    source_original = {
        "question_family": "business_object_impact_review",
        "primary_question_family": "business_object_impact_review",
        "question_families": [
            "business_object_impact_review",
            "segment_or_factor_attribution",
        ],
        "secondary_question_families": ["segment_or_factor_attribution"],
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    request = _resume_request(
        source_original,
        _complete_material_slots(claim_intents=["comparative_change"]),
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )
    request["clarification_resume_context"]["original_intent"][
        "primary_question_family"
    ] = "segment_or_factor_attribution"

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:question_families",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            request,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


@pytest.mark.parametrize(
    "axis",
    [
        "target_metrics",
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
        "scope",
    ],
)
def test_resume_rejects_mutable_prior_contract_material_axis_drift(axis):
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    mutable_contract = _typed_contract_payload(contract)
    if axis == "target_metrics":
        mutable_contract["target_metric_refs"] = ["paid_users"]
    elif axis == "requested_components":
        mutable_contract["scope"]["requested_metric_ids"] = [
            "paid_amount",
            "active_users",
        ]
    elif axis == "requested_dimensions":
        mutable_contract["scope"]["requested_dimension_ids"] = ["channel"]
    elif axis == "baselines":
        baseline = deepcopy(mutable_contract["resolved_windows"][0])
        baseline.update({"window_id": "previous_day", "role": "baseline"})
        mutable_contract["resolved_windows"] = (
            *mutable_contract["resolved_windows"],
            baseline,
        )
    elif axis == "context_sources":
        mutable_contract["dataset_requirements"] = (
            *mutable_contract["dataset_requirements"],
            "gameplay",
        )
    elif axis == "claim_intents":
        mutable_contract["claim_intents"] = ["observed_activity"]
    elif axis == "scope":
        mutable_contract["scope"]["type"] = "day"

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:prior_contract",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                {
                    "target_metric": "paid_amount",
                    "context_sources": [],
                    "claim_intents": ["comparative_change"],
                    "requested_dimensions": [],
                    "requested_components": [],
                    "scope": "full_sample",
                    "question": "source question",
                },
                {
                    **_complete_material_slots(
                        claim_intents=["comparative_change"]
                    ),
                    "scope": "full_sample",
                },
                analysis_contract=mutable_contract,
                authority_contract=contract,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_material_authority_builds_versioned_exact_signed_intent_and_route_slots():
    from bi_agent.conversation import clarification_authority

    original = {
        "question_family": "business_object_impact_review",
        "question_families": [
            "business_object_impact_review",
            "segment_or_factor_attribution",
        ],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": ["segment_or_factor_attribution"],
        "target_metric": "paid_amount",
        "requested_components": [],
        "requested_dimensions": ["channel"],
        "baseline_candidates": ["previous_day"],
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "scope": "full_sample",
    }
    route_slots = {
        **_complete_material_slots(
            target_metrics=["paid_amount", "paid_users"],
            requested_components=["active_users"],
            requested_dimensions=["channel"],
            baselines=["previous_day"],
            claim_intents=["comparative_change"],
        ),
        "diagnostic_tags": ["anomaly_scan_requested"],
        "scope": "full_sample",
    }

    envelope = clarification_authority.build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent=original,
        material_slots=route_slots,
        obligation_rejection_history=(
            {
                "action": "rejected",
                "capability": "event_impact",
                "reason": "diagnostic_question_family_incompatible",
            },
        ),
    )

    assert set(envelope) == {
        "schema_version",
        "source_run_id",
        "thread_id",
        "topic_id",
        "intent_material",
        "route_material_slots",
        "route_control",
        "material_authority_signature",
    }
    assert envelope["schema_version"] == "1"
    assert envelope["intent_material"] == {
        "primary_question_family": "business_object_impact_review",
        "question_families": [
            "business_object_impact_review",
            "segment_or_factor_attribution",
        ],
        "primary_target_metric": "paid_amount",
        "target_metrics": ["paid_amount", "paid_users"],
        "requested_components": [],
        "requested_dimensions": ["channel"],
        "baselines": ["previous_day"],
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "scope": "full_sample",
    }
    assert envelope["route_material_slots"] == route_slots
    assert envelope["route_control"] == {
        "obligation_rejection_history": [
            {
                "action": "rejected",
                "capability": "event_impact",
                "reason": "diagnostic_question_family_incompatible",
            }
        ]
    }
    assert clarification_authority.validate_material_authority(
        envelope,
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
    ) == envelope


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("unknown_top", "material_authority_shape_invalid"),
        ("unknown_intent", "material_authority_intent_shape_invalid"),
        ("unknown_route", "material_authority_route_shape_invalid"),
        (
            "unknown_route_control",
            "material_authority_route_control_shape_invalid",
        ),
        (
            "unknown_rejection_reason",
            "material_authority_rejection_history_invalid",
        ),
        (
            "duplicate_rejection",
            "material_authority_rejection_history_invalid",
        ),
        ("version", "material_authority_version_invalid"),
        ("signature", "material_authority_signature_invalid"),
        ("owner", "material_authority_owner_mismatch"),
    ],
)
def test_material_authority_exact_shape_version_signature_and_owner_fail_closed(
    mutation, reason
):
    from bi_agent.conversation import clarification_authority

    envelope = _signed_material_authority()
    if mutation == "unknown_top":
        envelope["unexpected"] = True
    elif mutation == "unknown_intent":
        envelope["intent_material"]["unexpected"] = True
    elif mutation == "unknown_route":
        envelope["route_material_slots"]["unexpected"] = True
    elif mutation == "unknown_route_control":
        envelope["route_control"]["unexpected"] = True
    elif mutation == "unknown_rejection_reason":
        envelope["route_control"]["obligation_rejection_history"] = [
            {
                "action": "rejected",
                "capability": "event_impact",
                "reason": "forged_reason",
            }
        ]
    elif mutation == "duplicate_rejection":
        rejection = {
            "action": "rejected",
            "capability": "event_impact",
            "reason": "diagnostic_question_family_incompatible",
        }
        envelope["route_control"]["obligation_rejection_history"] = [
            rejection,
            deepcopy(rejection),
        ]
    elif mutation == "version":
        envelope["schema_version"] = "2"
    elif mutation == "signature":
        envelope["material_authority_signature"] = "tampered"
    elif mutation == "owner":
        envelope["thread_id"] = "thread-other"

    with pytest.raises(EvidenceIntegrityError, match=reason):
        clarification_authority.validate_material_authority(
            envelope,
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
        )


@pytest.mark.parametrize(
    "axis,expected_reason",
    [
        (
            "question_families",
            "material_authority_contract_question_families_mismatch",
        ),
        (
            "target_metrics",
            "material_authority_contract_target_metrics_mismatch",
        ),
        (
            "route_target_metrics",
            "material_authority_contract_target_metrics_mismatch",
        ),
        ("scope", "material_authority_contract_scope_mismatch"),
    ],
)
def test_resume_authority_rejects_independently_signed_overlap_mismatch(
    axis, expected_reason
):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
        validate_clarification_resume_authority,
    )

    contract = _source_contract()
    family = (
        "segment_or_factor_attribution"
        if axis == "question_families"
        else "business_object_impact_review"
    )
    target_metric = "active_users" if axis == "target_metrics" else "paid_amount"
    scope = "custom_segment" if axis == "scope" else "full_sample"
    material_authority = build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent={
            "question_family": family,
            "question_families": [family],
            "primary_question_family": family,
            "secondary_question_families": [],
            "target_metric": target_metric,
            "requested_components": [],
            "requested_dimensions": [],
            "baseline_candidates": [],
            "context_sources": [],
            "claim_intents": [],
            "scope": scope,
        },
        material_slots={
            **_complete_material_slots(target_metrics=[target_metric]),
            "diagnostic_tags": [],
            "scope": scope,
        },
    )
    if axis == "route_target_metrics":
        from bi_agent.runtime.analysis_contracts import stable_contract_signature

        material_authority["route_material_slots"]["target_metrics"] = [
            "active_users"
        ]
        body = {
            key: value
            for key, value in material_authority.items()
            if key != "material_authority_signature"
        }
        material_authority["material_authority_signature"] = (
            stable_contract_signature(body)
        )
    choice = _choice()
    outcome = build_clarification_outcome(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        choice=choice,
    )

    with pytest.raises(EvidenceIntegrityError, match=expected_reason):
        validate_clarification_resume_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            choice=choice,
            outcome_ref=outcome["outcome_ref"],
            analysis_contract=contract,
            stored_contract_signature=contract["contract_signature"],
            analysis_run_id="run-source",
            run_status="waiting_for_clarification",
            run_thread_id="thread-source",
            run_topic_id="topic-source",
            clarification_outcome=outcome,
            outcome_run_id="run-source",
            outcome_thread_id="thread-source",
            outcome_topic_id="topic-source",
            material_authority=material_authority,
        )


@pytest.mark.parametrize(
    "contract_targets,material_targets,accepted",
    [
        (
            ("paid_amount", "paid_users"),
            ("paid_amount", "paid_users"),
            True,
        ),
        (
            ("paid_users", "paid_amount"),
            ("paid_users", "paid_amount"),
            True,
        ),
        (
            ("paid_amount", "paid_users"),
            ("paid_users", "paid_amount"),
            False,
        ),
    ],
)
def test_resume_authority_preserves_ordered_multi_target_overlap(
    contract_targets, material_targets, accepted
):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    contract = _source_contract_with_target_metrics(contract_targets)
    material = build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent={
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": material_targets[0],
            "requested_components": [],
            "requested_dimensions": [],
            "baseline_candidates": [],
            "context_sources": [],
            "claim_intents": [],
            "scope": "full_sample",
        },
        material_slots={
            **_complete_material_slots(target_metrics=list(material_targets)),
            "diagnostic_tags": [],
            "scope": "full_sample",
        },
    )

    if accepted:
        _validate_signed_authority_pair(contract, material)
    else:
        with pytest.raises(
            EvidenceIntegrityError,
            match="material_authority_contract_target_metrics_mismatch",
        ):
            _validate_signed_authority_pair(contract, material)


@pytest.mark.parametrize("failure", ["unresolvable", "ambiguous"])
def test_resume_authority_rejects_nonunique_contract_target_ref(failure):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    contract = _source_contract()
    if failure == "unresolvable":
        contract["target_metric_refs"] = ["contract:metric:missing"]
    else:
        duplicate = deepcopy(contract["metric_bindings"][0])
        duplicate["metric_id"] = "active_users"
        contract["metric_bindings"] = [
            *contract["metric_bindings"],
            duplicate,
        ]
    contract["contract_signature"] = analysis_contract_signature(contract)
    material = build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent={
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "requested_components": [],
            "requested_dimensions": [],
            "baseline_candidates": [],
            "context_sources": [],
            "claim_intents": [],
            "scope": "full_sample",
        },
        material_slots={
            **_complete_material_slots(),
            "diagnostic_tags": [],
            "scope": "full_sample",
        },
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_contract_target_metrics_unresolvable",
    ):
        _validate_signed_authority_pair(contract, material)


@pytest.mark.parametrize("material_scope", [None, "", {}, "full_sample"])
def test_resume_authority_normalizes_default_material_scope(material_scope):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    material = build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent={
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "requested_components": [],
            "requested_dimensions": [],
            "baseline_candidates": [],
            "context_sources": [],
            "claim_intents": [],
            "scope": material_scope,
        },
        material_slots={
            **_complete_material_slots(),
            "diagnostic_tags": [],
            "scope": material_scope,
        },
    )

    _validate_signed_authority_pair(_source_contract(), material)


def test_resume_authority_rejects_nested_material_scope_drift():
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    nested_scope = {
        "type": "full_sample",
        "segment": {"region": "lagos"},
    }
    material = build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent={
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "requested_components": [],
            "requested_dimensions": [],
            "baseline_candidates": [],
            "context_sources": [],
            "claim_intents": [],
            "scope": nested_scope,
        },
        material_slots={
            **_complete_material_slots(),
            "diagnostic_tags": [],
            "scope": nested_scope,
        },
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_contract_scope_mismatch",
    ):
        _validate_signed_authority_pair(_source_contract(), material)


def test_resume_carries_signed_route_rejection_once_and_ignores_mutable_injection():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    original = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "requested_components": [],
        "requested_dimensions": [],
        "baseline_candidates": [],
        "context_sources": [],
        "claim_intents": [],
        "scope": "full_sample",
        "question": "source question",
    }
    material_slots = {
        **_complete_material_slots(),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }
    signed_rejection = {
        "action": "rejected",
        "capability": "event_impact",
        "reason": "diagnostic_question_family_incompatible",
    }
    forged_rejection = {
        "action": "rejected",
        "capability": "revenue_health",
        "reason": "diagnostic_question_family_incompatible",
    }
    request = _resume_request(
        original,
        material_slots,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
        authority_obligation_rejection_history=(signed_rejection,),
    )
    request["clarification_resume_context"].update(
        {
            "accepted_graph": ["compare_periods"],
            "analysis_route": {
                "requested_nodes": ["compare_periods"],
                "analysis_requirements": deepcopy(material_slots),
                "obligation_resolution": {
                    "status": "resolved",
                    "mutation_history": [forged_rejection],
                },
            },
        }
    )
    intent = workflow._bind_clarification_resume_intent(
        {},
        request,
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )
    restored_history = tuple(
        intent.pop("_validated_obligation_rejection_history")
    )
    state = {
        "run_id": "run-route-control-resumed",
        "request": request,
        "intent": intent,
        "confirmed_understanding": {},
        "obligation_rejection_history": restored_history,
        "llm_calls": [],
        "checkpoint_events": [],
        "validator_results": [],
        "draft_claims": [],
        "evidence": [],
    }

    workflow._design_analysis_route(state)

    carried = state["analysis_route"]["obligation_resolution"].get(
        "mutation_history", []
    )
    assert restored_history == (signed_rejection,)
    assert carried.count(signed_rejection) == 1
    assert forged_rejection not in carried
    assert state["obligation_rejection_history"] == (signed_rejection,)


def test_resume_rejects_mutable_prior_contract_nonmaterial_semantic_drift():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    mutable_contract = _typed_contract_payload(contract)
    mutable_contract["permission_scope"] = "admin"

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:prior_contract",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                {
                    "target_metric": "paid_amount",
                    "context_sources": [],
                    "claim_intents": ["comparative_change"],
                    "requested_dimensions": [],
                    "requested_components": [],
                    "scope": "full_sample",
                    "question": "source question",
                },
                {
                    **_complete_material_slots(
                        claim_intents=["comparative_change"]
                    ),
                    "scope": "full_sample",
                },
                analysis_contract=mutable_contract,
                authority_contract=contract,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_resume_exact_material_rejects_source_run_authority_mismatch():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    contract["question_families"] = ["business_object_impact_review"]
    contract["contract_signature"] = analysis_contract_signature(contract)
    original = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    request = _resume_request(
        original,
        _complete_material_slots(),
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )
    request["clarification_resume_context"]["resume_run_id"] = "run-other"

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_authority_invalid:source_run_id",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            request,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


@pytest.mark.parametrize(
    "original_target,persisted_targets",
    [
        ("paid_users", ["paid_users", "paid_amount"]),
        ("paid_amount", ["paid_amount"]),
    ],
)
def test_resume_rejects_multi_target_reorder_or_deletion(
    original_target, persisted_targets
):
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract_with_target_metrics(
        ("paid_amount", "paid_users")
    )
    original = {
        "target_metric": original_target,
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    material = _complete_material_slots(
        target_metrics=persisted_targets,
        claim_intents=["comparative_change"],
    )
    authority_original = {
        **original,
        "target_metric": "paid_amount",
    }
    authority_material = _complete_material_slots(
        target_metrics=["paid_amount", "paid_users"],
        claim_intents=["comparative_change"],
    )

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:target_metrics",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                original,
                material,
                analysis_contract=_typed_contract_payload(contract),
                authority_contract=contract,
                authority_original_intent=authority_original,
                authority_material_slots=authority_material,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


@pytest.mark.parametrize(
    "axis",
    [
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
    ],
)
def test_resume_rejects_collusive_axis_deletion_from_immutable_contract(axis):
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = (
        _source_contract_with_window("previous_day", role="baseline")
        if axis == "baselines"
        else _source_contract()
    )
    if axis == "requested_components":
        contract["scope"] = {
            **contract["scope"],
            "requested_metric_ids": ["paid_amount", "active_users"],
        }
    elif axis == "requested_dimensions":
        contract["scope"] = {
            **contract["scope"],
            "requested_dimension_ids": ["channel"],
        }
    elif axis == "context_sources":
        contract["dataset_requirements"] = [
            *contract["dataset_requirements"],
            "gameplay",
        ]
    original = {
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": (
            [] if axis == "claim_intents" else ["comparative_change"]
        ),
        "requested_dimensions": [],
        "requested_components": [],
        "baseline_candidates": [],
        "question": "source question",
    }
    material = _complete_material_slots(
        claim_intents=(
            [] if axis == "claim_intents" else ["comparative_change"]
        )
    )
    authority_original = deepcopy(original)
    authority_material = deepcopy(material)
    authority_values = {
        "requested_components": ["active_users"],
        "requested_dimensions": ["channel"],
        "baselines": ["previous_day"],
        "context_sources": ["gameplay"],
        "claim_intents": ["comparative_change"],
    }
    authority_material[axis] = authority_values[axis]
    original_fields = {
        "requested_components": "requested_components",
        "requested_dimensions": "requested_dimensions",
        "baselines": "baseline_candidates",
        "context_sources": "context_sources",
        "claim_intents": "claim_intents",
    }
    authority_original[original_fields[axis]] = authority_values[axis]

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_conflict:{axis}",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                original,
                material,
                analysis_contract=_typed_contract_payload(contract),
                authority_contract=contract,
                authority_original_intent=authority_original,
                authority_material_slots=authority_material,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_resume_binds_primary_target_and_scope_from_immutable_contract():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract_with_target_metrics(
        ("paid_amount", "paid_users")
    )
    bound = workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            {
                "target_metric": "paid_amount",
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "requested_dimensions": [],
                "requested_components": [],
                "scope": "full_sample",
                "question": "source question",
            },
            _complete_material_slots(
                target_metrics=["paid_amount", "paid_users"],
                claim_intents=["comparative_change"],
            ),
            analysis_contract=_typed_contract_payload(contract),
            authority_contract=contract,
        ),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert bound["target_metric"] == "paid_amount"
    assert bound["scope"] == "full_sample"
    assert bound["question_families"] == ["business_object_impact_review"]


def test_resume_preserves_empty_explicit_components_with_driver_dependency_closure():
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    contract["scope"] = {
        **contract["scope"],
        "requested_metric_ids": ["paid_amount", "active_users"],
    }
    contract["contract_signature"] = analysis_contract_signature(contract)
    original = {
        "target_metric": "paid_amount",
        "requested_components": [],
        "requested_dimensions": [],
        "baseline_candidates": [],
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "scope": "full_sample",
        "question": "source question",
    }
    material = {
        **_complete_material_slots(claim_intents=["comparative_change"]),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }

    bound = workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            material,
            analysis_contract=_typed_contract_payload(contract),
            authority_contract=contract,
        ),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert bound["requested_components"] == []


@pytest.mark.parametrize("explicit_context", [[], ["gameplay"]])
def test_resume_preserves_metric_source_and_explicit_dual_role_context_separately(
    explicit_context,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    contract = _source_contract()
    contract["dataset_requirements"] = list(
        dict.fromkeys((*contract["dataset_requirements"], "gameplay"))
    )
    contract["contract_signature"] = analysis_contract_signature(contract)
    original = {
        "target_metric": "paid_amount",
        "requested_components": [],
        "requested_dimensions": [],
        "baseline_candidates": [],
        "context_sources": explicit_context,
        "claim_intents": ["comparative_change"],
        "scope": "full_sample",
        "question": "source question",
    }
    material = {
        **_complete_material_slots(
            context_sources=explicit_context,
            claim_intents=["comparative_change"],
        ),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }

    bound = workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            material,
            analysis_contract=_typed_contract_payload(contract),
            authority_contract=contract,
        ),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert bound["context_sources"] == explicit_context


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
    source_contract = _source_contract_with_accepted_claims()
    resume = {
        "resume_run_id": "run-source",
        "source_thread_id": "thread-source",
        "source_topic_id": "topic-source",
        "question": "source question",
        "original_intent": original,
        "material_slots": {
            "target_metrics": ["paid_amount"],
            "requested_components": [],
            "requested_dimensions": [],
            "baselines": [],
            "context_sources": [],
            "claim_intents": [
                "comparative_change",
                "contract_coverage_and_trust_boundary",
            ],
        },
        "analysis_contract": source_contract,
    }
    bound = workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            resume["material_slots"],
            analysis_contract=_typed_contract_payload(source_contract),
            authority_contract=source_contract,
        ),
        registry,
    )
    assert bound["claim_intents"] == ["comparative_change"]


def test_resume_allows_gap_scoped_requested_claim_without_promoting_bound_intent():
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
    material = _complete_material_slots(
        claim_intents=["comparative_change", "baseline_stability"]
    )
    source_contract = _source_contract_with_unsupported_claim()
    assert source_contract["claim_intents"] == ("comparative_change",)
    assert source_contract["contract_gaps"][-1]["affected_claim_types"] == (
        "baseline_stability",
    )

    bound = workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            material,
            analysis_contract=_typed_contract_payload(source_contract),
            authority_contract=source_contract,
        ),
        registry,
    )

    assert bound["claim_intents"] == ["comparative_change"]
    assert "baseline_stability" not in bound["claim_intents"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_authority",
        "contract_signature",
        "source_run",
        "thread",
        "topic",
        "outcome_signature",
        "outcome_ref",
        "choice",
        "gap_id",
        "gap_type",
        "gap_dataset",
        "gap_claim_count",
        "gap_owner",
        "gap_repair",
        "gap_clarification",
        "gap_diagnostic",
    ],
)
def test_resume_gap_scoped_claim_requires_valid_immutable_canonical_authority(
    mutation,
):
    from bi_agent.runtime import langgraph_workflow as workflow
    from bi_agent.runtime.analysis_contracts import (
        analysis_contract_signature,
        stable_contract_signature,
    )

    original = {
        "target_metric": "paid_amount",
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    material = _complete_material_slots(
        claim_intents=["comparative_change", "baseline_stability"]
    )
    contract = _source_contract_with_unsupported_claim()
    request = _resume_request(
        original,
        material,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )
    authority = request["accepted_terminal_gap_authority"]
    gap = authority["analysis_contract"]["contract_gaps"][-1]
    if mutation == "missing_authority":
        request.pop("accepted_terminal_gap_authority")
    elif mutation == "contract_signature":
        authority["analysis_contract_signature"] = "tampered"
    elif mutation == "source_run":
        authority["source_run_id"] = "run-other"
    elif mutation == "thread":
        authority["thread_id"] = "thread-other"
    elif mutation == "topic":
        authority["topic_id"] = "topic-other"
    elif mutation == "outcome_signature":
        authority["clarification_outcome"]["outcome_signature"] = "tampered"
    elif mutation == "outcome_ref":
        authority["clarification_outcome"]["outcome_ref"] = (
            "clarification-outcome:tampered"
        )
        outcome = authority["clarification_outcome"]
        outcome["outcome_signature"] = stable_contract_signature({
            key: value
            for key, value in outcome.items()
            if key != "outcome_signature"
        })
    elif mutation == "choice":
        authority["clarification_outcome"]["choice"] = {
            **authority["clarification_outcome"]["choice"],
            "choice_id": "tampered",
        }
        outcome = authority["clarification_outcome"]
        outcome["outcome_signature"] = stable_contract_signature({
            key: value
            for key, value in outcome.items()
            if key != "outcome_signature"
        })
    elif mutation == "gap_id":
        gap["gap_id"] = "claim_intent:baseline_stability:drift"
    elif mutation == "gap_type":
        gap["gap_type"] = "source_unbound"
    elif mutation == "gap_dataset":
        gap["dataset_id"] = "paid_order_success"
    elif mutation == "gap_claim_count":
        gap["affected_claim_types"] = [
            "baseline_stability",
            "comparative_change",
        ]
    elif mutation == "gap_owner":
        gap["owner"] = "runtime_owner"
    elif mutation == "gap_repair":
        gap["repair_options"] = ["clarify_claim_intent"]
    elif mutation == "gap_clarification":
        gap["requires_clarification"] = False
    elif mutation == "gap_diagnostic":
        gap["diagnostic_context"] = {"reason": "drift"}
    if mutation.startswith("gap_"):
        authority["analysis_contract_signature"] = analysis_contract_signature(
            authority["analysis_contract"]
        )

    with pytest.raises(workflow.WorkflowFailure) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            request,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


def test_resume_canonicalizes_object_baseline_and_allows_authorized_target_extra():
    from bi_agent.runtime import langgraph_workflow as workflow

    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [
            {"id": "previous_day", "label": "Previous day"},
            {"value": "previous_day"},
        ],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    source_contract = _source_contract_with_window(
        "previous_day",
        role="baseline",
        target_metrics=("paid_amount", "paid_users"),
    )
    source_contract["contract_signature"] = analysis_contract_signature(
        source_contract
    )
    resume = {
        "resume_run_id": "run-source",
        "source_thread_id": "thread-source",
        "source_topic_id": "topic-source",
        "question": "source question",
        "original_intent": original,
        "material_slots": {
            "target_metrics": ["paid_amount", "paid_users"],
            "requested_components": [],
            "requested_dimensions": [],
            "baselines": ["previous_day"],
            "context_sources": [],
            "claim_intents": ["comparative_change"],
        },
        "analysis_contract": source_contract,
    }
    workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            resume["material_slots"],
            analysis_contract=_typed_contract_payload(source_contract),
            authority_contract=source_contract,
        ),
        registry,
    )
    assert workflow._intent_material_slots(original)["baselines"] == [
        "previous_day"
    ]


def test_intent_material_slots_persist_complete_authority_axes():
    from bi_agent.runtime import langgraph_workflow as workflow

    assert workflow._intent_material_slots(
        {
            "target_metric": "paid_amount",
            "baseline_candidates": [],
            "scope": "full_sample",
        }
    ) == {
        "target_metrics": ["paid_amount"],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": [],
        "claim_intents": [],
        "scope": "full_sample",
    }


@pytest.mark.parametrize(
    "missing_axis",
    [
        "target_metrics",
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
    ],
)
def test_resume_material_schema_rejects_missing_authority_axis(missing_axis):
    from bi_agent.runtime import langgraph_workflow as workflow

    material = {
        "target_metrics": ["paid_amount"],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": [],
        "claim_intents": [],
    }
    material.pop(missing_axis)

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_invalid:{missing_axis}",
    ) as exc:
        workflow._validated_resume_material_slots(
            material,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


@pytest.mark.parametrize("material", [None, {}])
def test_resume_material_schema_rejects_absent_authority(material):
    from bi_agent.runtime import langgraph_workflow as workflow

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_invalid",
    ) as exc:
        workflow._validated_resume_material_slots(
            material,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


@pytest.mark.parametrize(
    "baselines",
    ["previous_day", [{}], ["yesterday"], ["previous_day", "previous_day"]],
)
def test_resume_material_schema_rejects_malformed_baselines(baselines):
    from bi_agent.runtime import langgraph_workflow as workflow

    material = {
        "target_metrics": ["paid_amount"],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": baselines,
        "context_sources": [],
        "claim_intents": [],
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_invalid:baselines",
    ) as exc:
        workflow._validated_resume_material_slots(
            material,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


def test_resume_accepts_source_contract_authorized_component_expansion():
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["previous_day"],
        "context_sources": ["gameplay"],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": ["active_users"],
        "question": "source question",
    }
    contract = _source_contract_with_window("previous_day", role="baseline")
    contract["scope"] = {
        **contract["scope"],
        "requested_metric_ids": [
            "paid_amount",
            "active_users",
            "player_bet_amount",
        ],
    }
    contract["dataset_requirements"] = [
        *contract["dataset_requirements"],
        "gameplay",
    ]
    contract["contract_signature"] = analysis_contract_signature(contract)
    material = _complete_material_slots(
        requested_components=["active_users", "player_bet_amount"],
        baselines=["previous_day"],
        context_sources=["gameplay"],
        claim_intents=["comparative_change"],
    )

    bound = workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            material,
            analysis_contract=_typed_contract_payload(contract),
            authority_contract=contract,
        ),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert bound["requested_components"] == ["active_users"]


@pytest.mark.parametrize(
    "axis,persisted_values",
    [
        ("target_metrics", []),
        ("requested_components", []),
        ("requested_dimensions", []),
        ("context_sources", []),
        ("claim_intents", []),
    ],
)
def test_resume_rejects_removing_original_material_axis(axis, persisted_values):
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [],
        "context_sources": ["gameplay"],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": ["channel"],
        "requested_components": ["active_users"],
        "question": "source question",
    }
    material = _complete_material_slots(
        requested_components=["active_users"],
        requested_dimensions=["channel"],
        context_sources=["gameplay"],
        claim_intents=["comparative_change"],
    )
    material[axis] = persisted_values

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_conflict:{axis}",
    ) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(original, material),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


def test_resume_accepts_multiple_source_contract_authorized_axis_expansions():
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    contract = _source_contract_with_target_metrics(
        ("paid_amount", "player_bet_amount")
    )
    contract["scope"] = {
        **contract["scope"],
        "requested_metric_ids": [
            "paid_amount",
            "player_bet_amount",
            "active_users",
        ],
        "requested_dimension_ids": ["gameplay"],
    }
    contract["dataset_requirements"] = list(
        dict.fromkeys((*contract["dataset_requirements"], "gameplay"))
    )
    contract["claim_intents"] = ["observed_activity"]
    contract["contract_signature"] = analysis_contract_signature(contract)
    material = _complete_material_slots(
        target_metrics=["paid_amount", "player_bet_amount"],
        requested_components=["active_users"],
        requested_dimensions=["gameplay"],
        context_sources=["gameplay"],
        claim_intents=["observed_activity"],
    )

    workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            material,
            analysis_contract=_typed_contract_payload(contract),
            authority_contract=contract,
        ),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )


@pytest.mark.parametrize(
    "axis,material_update",
    [
        ("target_metrics", {"target_metrics": ["paid_amount", "paid_users"]}),
        (
            "requested_components",
            {"requested_components": ["paid_users"]},
        ),
        ("requested_dimensions", {"requested_dimensions": ["channel"]}),
        ("context_sources", {"context_sources": ["gameplay"]}),
        (
            "claim_intents",
            {"claim_intents": ["contract_coverage_and_trust_boundary"]},
        ),
    ],
)
def test_resume_rejects_source_contract_unauthorized_axis_extra(
    axis, material_update
):
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    contract = _source_contract()
    contract.pop("contract_signature", None)
    material = _complete_material_slots(**material_update)

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_conflict:{axis}",
    ) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(original, material, analysis_contract=contract),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


@pytest.mark.parametrize("contract_state", ["missing", "malformed", "wrong_id"])
def test_resume_component_extra_requires_valid_source_contract(contract_state):
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    material = _complete_material_slots(requested_components=["paid_users"])
    contract = None
    if contract_state != "missing":
        contract = _source_contract()
        contract.pop("contract_signature", None)
        if contract_state == "malformed":
            contract["metric_bindings"] = [{}]
        else:
            contract["analysis_contract_id"] = "analysis:run-other:1"

    with pytest.raises(
        workflow.WorkflowFailure,
        match=(
            "clarification_resume_material_slots_conflict:"
            "requested_components"
        ),
    ) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(original, material, analysis_contract=contract),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


def test_resume_accepts_contract_authorized_baseline_expansion():
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["前日", "上周同日"],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    material = _complete_material_slots(
        baselines=["same_weekday_last_week"],
        claim_intents=["comparative_change"],
    )
    contract = _source_contract_with_window(
        "same_weekday_last_week", role="baseline"
    )
    contract["contract_signature"] = analysis_contract_signature(contract)

    workflow._bind_clarification_resume_intent(
        {},
        _resume_request(
            original,
            material,
            analysis_contract=_typed_contract_payload(contract),
            authority_contract=contract,
        ),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )


def test_resume_accepts_reordered_original_canonical_baselines():
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["previous_day", "same_weekday_last_week"],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    material = _complete_material_slots(
        baselines=["same_weekday_last_week", "previous_day"]
    )

    workflow._bind_clarification_resume_intent(
        {},
        _resume_request(original, material),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )


def test_resume_rejects_removing_original_canonical_baseline():
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["previous_day", "same_weekday_last_week"],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:baselines",
    ) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                original,
                _complete_material_slots(baselines=["previous_day"]),
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


def test_resume_keeps_scope_exact_when_baseline_is_unchanged():
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [],
        "scope": "day",
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    material = _complete_material_slots()
    material["scope"] = "full_sample"

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:scope",
    ) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(original, material),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


@pytest.mark.parametrize(
    "contract_state",
    ["missing", "malformed", "wrong_id", "unresolved", "reference_only"],
)
def test_resume_baseline_extra_requires_resolved_source_baseline(contract_state):
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    contract = None
    if contract_state == "malformed":
        contract = _source_contract_with_window(
            "same_weekday_last_week", role="baseline"
        )
        contract["resolved_windows"] = [{}]
    elif contract_state == "wrong_id":
        contract = _source_contract_with_window(
            "same_weekday_last_week", role="baseline"
        )
        contract["analysis_contract_id"] = "analysis:run-other:1"
    elif contract_state == "unresolved":
        contract = _source_contract()
        contract.pop("contract_signature", None)
    elif contract_state == "reference_only":
        contract = _source_contract_with_window(
            "same_weekday_last_week", role="reference"
        )

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:baselines",
    ) as exc:
        workflow._bind_clarification_resume_intent(
            {},
            _resume_request(
                original,
                _complete_material_slots(
                    baselines=["same_weekday_last_week"]
                ),
                analysis_contract=contract,
            ),
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

    assert exc.value.failure_type == "contract"


def _source_contract(run_id="run-source"):
    outcome = compile_analysis_contract(
        run_id=run_id,
        proposal={
            "question_families": ["business_object_impact_review"],
            "target_metrics": ["paid_amount"],
        },
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


def _source_contract_with_unsupported_claim(run_id="run-source"):
    outcome = compile_analysis_contract(
        run_id=run_id,
        proposal={
            "question_families": ["business_object_impact_review"],
            "target_metrics": ["paid_amount"],
            "claim_intents": ["comparative_change", "baseline_stability"],
        },
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


def _source_contract_with_accepted_claims(run_id="run-source"):
    outcome = compile_analysis_contract(
        run_id=run_id,
        proposal={
            "question_families": ["business_object_impact_review"],
            "target_metrics": ["paid_amount"],
            "claim_intents": [
                "comparative_change",
                "contract_coverage_and_trust_boundary",
            ],
        },
        accepted_capabilities=(
            "compare_periods",
            "data_quality_profile",
            "answer_verify",
        ),
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


def _source_contract_with_target_metrics(
    target_metrics, run_id="run-source"
):
    preferred_sources = {
        "paid_amount": "paid_order_success",
        "paid_users": "paid_order_success",
        "active_users": "market_dashboard",
        "player_bet_amount": "gameplay",
    }
    outcome = compile_analysis_contract(
        run_id=run_id,
        proposal={
            "question_families": ["business_object_impact_review"],
            "target_metrics": list(target_metrics),
            "metric_dataset_overrides": {
                metric_id: preferred_sources[metric_id]
                for metric_id in target_metrics
                if metric_id in preferred_sources
            },
        },
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


def _typed_contract_payload(contract):
    return {
        key: deepcopy(value)
        for key, value in contract.items()
        if key != "contract_signature"
    }


def _source_contract_with_window(
    window_id, *, role, target_metrics=("paid_amount",)
):
    payload = _source_contract_with_target_metrics(target_metrics)
    payload.pop("contract_signature", None)
    window = deepcopy(payload["resolved_windows"][0])
    window.update({"window_id": window_id, "role": role})
    payload["resolved_windows"] = [*payload["resolved_windows"], window]
    return payload


def _complete_material_slots(
    *,
    target_metrics=None,
    requested_components=None,
    requested_dimensions=None,
    baselines=None,
    context_sources=None,
    claim_intents=None,
):
    return {
        "target_metrics": (
            ["paid_amount"] if target_metrics is None else target_metrics
        ),
        "requested_components": (
            [] if requested_components is None else requested_components
        ),
        "requested_dimensions": (
            [] if requested_dimensions is None else requested_dimensions
        ),
        "baselines": [] if baselines is None else baselines,
        "context_sources": [] if context_sources is None else context_sources,
        "claim_intents": [] if claim_intents is None else claim_intents,
    }


def _signed_material_authority(
    original_intent=None,
    material_slots=None,
    *,
    source_run_id="run-source",
    thread_id="thread-source",
    topic_id="topic-source",
    obligation_rejection_history=(),
):
    from bi_agent.runtime.analysis_contracts import stable_contract_signature

    original = deepcopy(original_intent or {})
    families = list(
        original.get("question_families")
        or ["business_object_impact_review"]
    )
    primary_family = str(
        original.get("primary_question_family")
        or original.get("question_family")
        or families[0]
    )
    slots = {
        **_complete_material_slots(),
        "diagnostic_tags": [],
        "scope": original.get("scope", "full_sample"),
        **deepcopy(material_slots or {}),
    }
    primary_target = str(
        original.get("target_metric")
        or next(iter(slots["target_metrics"]), "paid_amount")
    )
    body = {
        "schema_version": "1",
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "intent_material": {
            "primary_question_family": primary_family,
            "question_families": families,
            "primary_target_metric": primary_target,
            "target_metrics": list(slots["target_metrics"]),
            "requested_components": list(
                original.get("requested_components") or ()
            ),
            "requested_dimensions": list(
                original.get("requested_dimensions") or ()
            ),
            "baselines": list(original.get("baseline_candidates") or ()),
            "context_sources": list(original.get("context_sources") or ()),
            "claim_intents": list(original.get("claim_intents") or ()),
            "scope": original.get("scope", "full_sample"),
        },
        "route_material_slots": slots,
        "route_control": {
            "obligation_rejection_history": list(
                deepcopy(obligation_rejection_history)
            )
        },
    }
    return {
        **body,
        "material_authority_signature": stable_contract_signature(body),
    }


def _resume_request(
    original_intent,
    material_slots,
    *,
    analysis_contract=None,
    authority_contract=None,
    authority_original_intent=None,
    authority_material_slots=None,
    authority_obligation_rejection_history=(),
):
    canonical_original = deepcopy(original_intent)
    authority_families = tuple(
        str(family)
        for family in (
            authority_contract.get("question_families")
            if isinstance(authority_contract, dict)
            else ()
        ) or ()
        if str(family)
    )
    if authority_families:
        canonical_original.setdefault("question_family", authority_families[0])
        canonical_original.setdefault(
            "question_families", list(authority_families)
        )
        canonical_original.setdefault(
            "primary_question_family", authority_families[0]
        )
        canonical_original.setdefault(
            "secondary_question_families", list(authority_families[1:])
        )
    resume_context = {
        "resume_run_id": "run-source",
        "source_thread_id": "thread-source",
        "source_topic_id": "topic-source",
        "question": "source question",
        "original_intent": canonical_original,
        "material_slots": material_slots,
    }
    if analysis_contract is not None:
        resume_context["analysis_contract"] = analysis_contract
    request = {
        "thread_id": "thread-source",
        "topic_id": "topic-source",
        "question": "source question",
        "clarification_resume_context": resume_context,
    }
    if authority_contract is not None:
        from bi_agent.conversation.clarification_authority import (
            build_material_authority,
        )

        choice = _choice()
        contract = deepcopy(authority_contract)
        contract.pop("contract_signature", None)
        source_original = deepcopy(
            canonical_original
            if authority_original_intent is None
            else authority_original_intent
        )
        source_original.setdefault("question_family", authority_families[0])
        source_original.setdefault(
            "question_families", list(authority_families)
        )
        source_original.setdefault(
            "primary_question_family", authority_families[0]
        )
        source_original.setdefault(
            "secondary_question_families", list(authority_families[1:])
        )
        material_authority = build_material_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            original_intent=source_original,
            material_slots=(
                material_slots
                if authority_material_slots is None
                else authority_material_slots
            ),
            obligation_rejection_history=(
                authority_obligation_rejection_history
            ),
        )
        outcome = build_clarification_outcome(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            choice=choice,
        )
        request["accepted_degradation_choice"] = choice
        request["accepted_terminal_gap_authority"] = {
            "source_run_id": "run-source",
            "thread_id": "thread-source",
            "topic_id": "topic-source",
            "analysis_contract": contract,
            "analysis_contract_signature": analysis_contract_signature(contract),
            "material_authority": material_authority,
            "clarification_outcome": outcome,
        }
        request["clarification_outcome_ref"] = outcome["outcome_ref"]
    return request


def _choice(run_id="run-source"):
    return {
        "choice_id": "continue-boundary",
        "action_kind": "continue_with_boundary_only",
        "business_label": "保留边界并继续",
        "affected_capabilities": ["compare_periods"],
        "source_run_id": run_id,
    }


def _validate_signed_authority_pair(contract, material_authority):
    from bi_agent.conversation.clarification_authority import (
        validate_clarification_resume_authority,
    )

    choice = _choice()
    outcome = build_clarification_outcome(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        choice=choice,
    )
    return validate_clarification_resume_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        choice=choice,
        outcome_ref=outcome["outcome_ref"],
        analysis_contract=contract,
        stored_contract_signature=contract["contract_signature"],
        analysis_run_id="run-source",
        run_status="waiting_for_clarification",
        run_thread_id="thread-source",
        run_topic_id="topic-source",
        clarification_outcome=outcome,
        outcome_run_id="run-source",
        outcome_thread_id="thread-source",
        outcome_topic_id="topic-source",
        material_authority=material_authority,
    )


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
    material_authority = _signed_material_authority(
        {
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "primary_question_family": "business_object_impact_review",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "requested_components": [],
            "requested_dimensions": [],
            "baseline_candidates": [],
            "context_sources": [],
            "claim_intents": ["comparative_change"],
            "scope": "full_sample",
        },
        _complete_material_slots(claim_intents=["comparative_change"]),
        thread_id="thread-1",
        topic_id="topic-1",
    )
    store.upsert_run(
        "run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        status="waiting_for_clarification",
        request={
            "analysis_contract": {"tampered": "request-context-only"},
            "material_authority": material_authority,
        },
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
    assert authority["material_authority"] == store.runs["run-source"][
        "request"
    ]["material_authority"]
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
        ("material_missing", "material_authority_missing"),
        ("material_signature", "material_authority_signature_invalid"),
        ("material_owner", "material_authority_owner_mismatch"),
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
    if mutation == "material_missing":
        store.runs["run-source"]["request"].pop("material_authority")
    if mutation == "material_signature":
        store.runs["run-source"]["request"]["material_authority"][
            "material_authority_signature"
        ] = "tampered"
    if mutation == "material_owner":
        store.runs["run-source"]["request"]["material_authority"][
            "topic_id"
        ] = "topic-other"
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
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
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
        "run_request": json.dumps(
            {"material_authority": material_authority}
        ),
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
    assert "r.request AS run_request" in sql
    assert resolved["clarification_outcome"]["outcome_ref"] == outcome_ref
    assert resolved["material_authority"] == material_authority


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
                        "requested_nodes": ["segment_contribution"],
                        "obligation_resolution": {
                            "status": "resolved",
                            "mutation_history": [
                                {
                                    "action": "rejected",
                                    "capability": "event_impact",
                                    "reason": (
                                        "diagnostic_question_family_incompatible"
                                    ),
                                }
                            ],
                        },
                    },
                    "original_intent": {
                        "question_family": "segment_or_factor_attribution",
                        "question_families": [
                            "segment_or_factor_attribution"
                        ],
                        "primary_question_family": (
                            "segment_or_factor_attribution"
                        ),
                        "secondary_question_families": [],
                        "target_metric": "paid_amount",
                        "requested_components": [],
                        "requested_dimensions": ["channel"],
                        "baseline_candidates": [],
                        "context_sources": [],
                        "claim_intents": [
                            "segment_contribution_or_mix_shift"
                        ],
                        "scope": "full_sample",
                    },
                    "material_slots": {
                        **_complete_material_slots(
                            requested_dimensions=["channel"],
                            claim_intents=[
                                "segment_contribution_or_mix_shift"
                            ],
                        ),
                        "diagnostic_tags": [],
                        "scope": "full_sample",
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

    store = InMemoryConversationStore()
    core = ConversationAgentCore(store, workflow_runner=workflow)
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
    persisted_material_authority = store.runs["run-authority-source"][
        "request"
    ]["material_authority"]
    assert persisted_material_authority["schema_version"] == "1"
    assert persisted_material_authority["source_run_id"] == (
        "run-authority-source"
    )
    assert persisted_material_authority["intent_material"][
        "requested_components"
    ] == []
    assert persisted_material_authority["route_control"] == {
        "obligation_rejection_history": [
            {
                "action": "rejected",
                "capability": "event_impact",
                "reason": "diagnostic_question_family_incompatible",
            }
        ]
    }
    assert resumed["status"] == "failed"
    authority = calls[1]["accepted_terminal_gap_authority"]
    assert authority["source_run_id"] == "run-authority-source"
    assert authority["analysis_contract"]["analysis_contract_id"] == (
        "analysis:run-authority-source:1"
    )
    assert authority["clarification_outcome"]["choice"] == (
        calls[1]["accepted_degradation_choice"]
    )
    assert authority["material_authority"] == persisted_material_authority
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
    original_intent = {
        "question_family": "segment_or_factor_attribution",
        "question_families": ["segment_or_factor_attribution"],
        "primary_question_family": "segment_or_factor_attribution",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "requested_components": [],
        "requested_dimensions": ["channel"],
        "baseline_candidates": ["previous_day"],
        "context_sources": ["gameplay"],
        "claim_intents": ["segment_contribution_or_mix_shift"],
        "scope": "full_sample",
        "question": "昨天渠道贡献如何？",
    }
    material_slots = {
        **_complete_material_slots(
            requested_dimensions=["channel"],
            baselines=["previous_day"],
            context_sources=["gameplay"],
            claim_intents=["segment_contribution_or_mix_shift"],
        ),
        "diagnostic_tags": [],
        "scope": "full_sample",
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
            "original_intent": original_intent,
            "material_slots": material_slots,
            "material_authority": _signed_material_authority(
                original_intent,
                material_slots,
                source_run_id="run-segment-source",
                thread_id="thread-segment-closure",
                topic_id=topic.topic_id,
            ),
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

    carried, outcome_ref, authority_claim_intents = (
        _accepted_terminal_gap_authority({
            "question_families": calls[0]["accepted_terminal_gap_authority"][
                "material_authority"
            ]["intent_material"]["question_families"],
            "target_metrics": calls[0]["accepted_terminal_gap_authority"][
                "material_authority"
            ]["route_material_slots"]["target_metrics"],
            "scope": calls[0]["accepted_terminal_gap_authority"][
                "material_authority"
            ]["route_material_slots"]["scope"],
            "accepted_degradation_choice": effective,
            "accepted_terminal_gap_authority": calls[0][
                "accepted_terminal_gap_authority"
            ],
            "resume_thread_id": "thread-segment-closure",
            "resume_topic_id": topic.topic_id,
        })
    )
    assert outcome_ref == calls[0]["clarification_outcome_ref"]
    assert authority_claim_intents == ("segment_contribution_or_mix_shift",)
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
