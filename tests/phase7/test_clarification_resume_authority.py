from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from types import MappingProxyType

import pytest

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    _build_clarification_source_envelope,
)
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
        lambda state, node, payload, **kwargs: {
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
    clarification = {
        "questions": [{
            "question": "choose",
            "options": ["continue source topic"],
        }],
    }
    store.upsert_run(
        source_run_id,
        thread_id="thread-query-gap-roundtrip",
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "clarification_source_envelope": (
                _build_clarification_source_envelope(
                    source_run_id=source_run_id,
                    source_thread_id="thread-query-gap-roundtrip",
                    source_topic_id=topic.topic_id,
                    source_owner_id="analyst",
                    question="source business question",
                    analysis_context={},
                    analysis_contract=source_contract,
                    original_intent=original_intent,
                    material_slots=material_slots,
                    clarification=clarification,
                )
            ),
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
        "execution_material",
        "route_control",
        "material_authority_signature",
    }
    assert envelope["schema_version"] == "3"
    assert envelope["execution_material"] is None
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
        "time_window": None,
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
    ("time_window", "baseline_candidates", "canonical_baseline"),
    [
        (
            {"target": "yesterday", "baseline": "past 7 days"},
            [
                {
                    "description": "近7日均值",
                    "type": "rolling_average",
                    "window": 7,
                }
            ],
            "rolling_7_day_baseline",
        ),
        (
            {
                "target": "latest complete day",
                "baseline": {"description": "上周同日"},
            },
            [{"description": "上周同日", "ref": "last_week_same_day"}],
            "same_weekday_last_week",
        ),
    ],
)
def test_material_authority_uses_reviewed_route_baselines_over_narrative_intent(
    time_window, baseline_candidates, canonical_baseline
):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    material = build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent={
            "question_family": "custom_baseline_comparison",
            "question_families": ["custom_baseline_comparison"],
            "primary_question_family": "custom_baseline_comparison",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "time_window": time_window,
            "baseline_candidates": baseline_candidates,
            "requested_components": [],
            "requested_dimensions": [],
            "context_sources": [],
            "claim_intents": ["comparative_change"],
            "scope": "full_sample",
        },
        material_slots={
            **_complete_material_slots(
                baselines=[canonical_baseline],
                claim_intents=["comparative_change"],
            ),
            "diagnostic_tags": [],
            "scope": "full_sample",
        },
    )

    assert material["intent_material"]["baselines"] == [canonical_baseline]
    assert material["route_material_slots"]["baselines"] == [
        canonical_baseline
    ]


def test_material_authority_signature_ignores_narrative_baseline_variants():
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    def build(baseline_candidates):
        return build_material_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            original_intent={
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "primary_question_family": "custom_baseline_comparison",
                "secondary_question_families": [],
                "target_metric": "paid_amount",
                "time_window": {
                    "target": "yesterday",
                    "baseline": "previous business day",
                },
                "baseline_candidates": baseline_candidates,
                "requested_components": [],
                "requested_dimensions": [],
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "scope": "full_sample",
            },
            material_slots={
                **_complete_material_slots(
                    baselines=["previous_day"],
                    claim_intents=["comparative_change"],
                ),
                "diagnostic_tags": [],
                "scope": "full_sample",
            },
        )

    narrative = build(["前日"])
    machine_shaped = build([{"baseline_id": "previous_day"}])

    assert narrative == machine_shaped


@pytest.mark.parametrize(
    "route_baselines",
    [
        [],
        ["previous_day"],
        ["previous_day", "same_weekday_last_week"],
    ],
)
def test_material_authority_rejects_ambiguous_narrative_baseline_translation(
    route_baselines,
):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_baselines_invalid",
    ):
        build_material_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            original_intent={
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "primary_question_family": "custom_baseline_comparison",
                "secondary_question_families": [],
                "target_metric": "paid_amount",
                "time_window": "latest_complete_day",
                "baseline_candidates": [
                    {"description": "自定义业务对照窗口"}
                ],
                "requested_components": [],
                "requested_dimensions": [],
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "scope": "full_sample",
            },
            material_slots={
                **_complete_material_slots(
                    baselines=route_baselines,
                    claim_intents=["comparative_change"],
                ),
                "diagnostic_tags": [],
                "scope": "full_sample",
            },
        )


def test_material_authority_rejects_conflicting_ids_in_one_baseline_structure():
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_baselines_invalid",
    ):
        build_material_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            original_intent={
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "primary_question_family": "custom_baseline_comparison",
                "secondary_question_families": [],
                "target_metric": "paid_amount",
                "time_window": "latest_complete_day",
                "baseline_candidates": [{
                    "baseline_id": "previous_day",
                    "ref": "last_week_same_day",
                }],
                "requested_components": [],
                "requested_dimensions": [],
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "scope": "full_sample",
            },
            material_slots={
                **_complete_material_slots(
                    baselines=[
                        "previous_day",
                        "same_weekday_last_week",
                    ],
                    claim_intents=["comparative_change"],
                ),
                "diagnostic_tags": [],
                "scope": "full_sample",
            },
        )


def test_material_authority_rejects_mapped_candidate_outside_reviewed_route():
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_baselines_invalid",
    ):
        build_material_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            original_intent={
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "primary_question_family": "custom_baseline_comparison",
                "secondary_question_families": [],
                "target_metric": "paid_amount",
                "time_window": "latest_complete_day",
                "baseline_candidates": [
                    "previous_day",
                    {"ref": "last_week_same_day"},
                ],
                "requested_components": [],
                "requested_dimensions": [],
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "scope": "full_sample",
            },
            material_slots={
                **_complete_material_slots(
                    baselines=["previous_day"],
                    claim_intents=["comparative_change"],
                ),
                "diagnostic_tags": [],
                "scope": "full_sample",
            },
        )


@pytest.mark.parametrize(
    ("axis", "tampered_value"),
    [
        ("baseline_candidates", ["same_weekday_last_week"]),
        (
            "time_window",
            {"target": "today", "baseline": "previous business day"},
        ),
    ],
)
def test_terminal_resume_rejects_mutable_baseline_or_time_window_tamper(
    axis, tampered_value
):
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["previous_day"],
        "time_window": {
            "target": "yesterday",
            "baseline": "previous business day",
        },
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "scope": "full_sample",
        "question": "source question",
    }
    material = {
        **_complete_material_slots(
            baselines=["previous_day"],
            claim_intents=["comparative_change"],
        ),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }
    contract = _source_contract_with_window("previous_day", role="baseline")
    contract["contract_signature"] = analysis_contract_signature(contract)
    request = _resume_request(
        original,
        material,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )
    request["clarification_resume_context"]["original_intent"][axis] = (
        tampered_value
    )

    with pytest.raises(
        workflow.WorkflowFailure,
        match=f"clarification_resume_material_slots_conflict:{'baselines' if axis == 'baseline_candidates' else 'time_window'}",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            request,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_terminal_resume_keeps_explicit_intent_baseline_separate_from_route_expansion():
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["previous_day"],
        "time_window": {
            "target": "yesterday",
            "baseline": "previous business day",
        },
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "scope": "full_sample",
        "question": "source question",
    }
    route_baselines = ["previous_day", "rolling_7_day_baseline"]
    material = {
        **_complete_material_slots(
            baselines=route_baselines,
            claim_intents=["comparative_change"],
        ),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }
    contract = _source_contract_with_window("previous_day", role="baseline")
    rolling = deepcopy(contract["resolved_windows"][-1])
    rolling["window_id"] = "rolling_7_day_baseline"
    contract["resolved_windows"].append(rolling)
    contract["contract_signature"] = analysis_contract_signature(contract)
    request = _resume_request(
        original,
        material,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )

    signed = request["accepted_terminal_gap_authority"]["material_authority"]
    assert signed["intent_material"]["baselines"] == ["previous_day"]
    assert signed["route_material_slots"]["baselines"] == route_baselines

    bound = workflow._bind_clarification_resume_intent(
        {},
        request,
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )
    assert bound["baseline_candidates"] == route_baselines


@pytest.mark.parametrize(
    ("time_window", "baseline_candidates", "canonical_baseline"),
    [
        (
            {"target": "yesterday", "baseline": "past 7 days"},
            [
                {
                    "description": "近7日均值",
                    "type": "rolling_average",
                    "window": 7,
                }
            ],
            "rolling_7_day_baseline",
        ),
        (
            "latest_complete_day",
            [{"description": "上周同日", "ref": "last_week_same_day"}],
            "same_weekday_last_week",
        ),
    ],
)
def test_terminal_resume_binds_real_llm_baseline_shapes_to_reviewed_route(
    time_window, baseline_candidates, canonical_baseline
):
    from bi_agent.runtime import langgraph_workflow as workflow

    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": baseline_candidates,
        "time_window": time_window,
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "scope": "full_sample",
        "question": "source question",
    }
    material = {
        **_complete_material_slots(
            baselines=[canonical_baseline],
            claim_intents=["comparative_change"],
        ),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }
    contract = _source_contract_with_window(canonical_baseline, role="baseline")
    contract["contract_signature"] = analysis_contract_signature(contract)
    request = _resume_request(
        original,
        material,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )

    bound = workflow._bind_clarification_resume_intent(
        {},
        request,
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert bound["baseline_candidates"] == [canonical_baseline]
    assert bound["time_window"] == time_window


def test_terminal_resume_binds_multi_baseline_llm_structures_as_reviewed_set():
    from bi_agent.runtime import langgraph_workflow as workflow

    route_baselines = [
        "rolling_7_day_baseline",
        "same_weekday_last_week",
    ]
    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": [
            {
                "description": "近7日均值",
                "type": "rolling_average",
                "window": 7,
            },
            {"description": "上周同日", "ref": "last_week_same_day"},
        ],
        "time_window": "latest_complete_day",
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "scope": "full_sample",
        "question": "source question",
    }
    material = {
        **_complete_material_slots(
            baselines=route_baselines,
            claim_intents=["comparative_change"],
        ),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }
    contract = _source_contract_with_window(
        "rolling_7_day_baseline",
        role="baseline",
    )
    same_weekday = deepcopy(contract["resolved_windows"][-1])
    same_weekday["window_id"] = "same_weekday_last_week"
    contract["resolved_windows"].append(same_weekday)
    contract["contract_signature"] = analysis_contract_signature(contract)

    request = _resume_request(
        original,
        material,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )
    signed = request["accepted_terminal_gap_authority"]["material_authority"]
    assert signed["intent_material"]["baselines"] == route_baselines

    bound = workflow._bind_clarification_resume_intent(
        {},
        request,
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )
    assert bound["baseline_candidates"] == route_baselines


def test_material_authority_preserves_mixed_canonical_and_typed_baselines_order_independently():
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    typed = {
        "description": "近7日均值",
        "type": "rolling_average",
        "window": 7,
    }
    route_baselines = ["previous_day", "rolling_7_day_baseline"]

    def build(candidates):
        return build_material_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            original_intent={
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "primary_question_family": "custom_baseline_comparison",
                "secondary_question_families": [],
                "target_metric": "paid_amount",
                "time_window": "latest_complete_day",
                "baseline_candidates": candidates,
                "requested_components": [],
                "requested_dimensions": [],
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "scope": "full_sample",
            },
            material_slots={
                **_complete_material_slots(
                    baselines=route_baselines,
                    claim_intents=["comparative_change"],
                ),
                "diagnostic_tags": [],
                "scope": "full_sample",
            },
        )

    original_order = build(["previous_day", typed])
    reversed_order = build([typed, "previous_day"])

    assert original_order["intent_material"]["baselines"] == route_baselines
    assert reversed_order == original_order


def test_terminal_resume_rejects_removing_one_mixed_explicit_baseline():
    from bi_agent.runtime import langgraph_workflow as workflow

    typed = {
        "description": "近7日均值",
        "type": "rolling_average",
        "window": 7,
    }
    route_baselines = ["previous_day", "rolling_7_day_baseline"]
    original = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["previous_day", typed],
        "time_window": "latest_complete_day",
        "context_sources": [],
        "claim_intents": ["comparative_change"],
        "requested_dimensions": [],
        "requested_components": [],
        "scope": "full_sample",
        "question": "source question",
    }
    material = {
        **_complete_material_slots(
            baselines=route_baselines,
            claim_intents=["comparative_change"],
        ),
        "diagnostic_tags": [],
        "scope": "full_sample",
    }
    contract = _source_contract_with_window("previous_day", role="baseline")
    rolling = deepcopy(contract["resolved_windows"][-1])
    rolling["window_id"] = "rolling_7_day_baseline"
    contract["resolved_windows"].append(rolling)
    contract["contract_signature"] = analysis_contract_signature(contract)
    request = _resume_request(
        original,
        material,
        analysis_contract=_typed_contract_payload(contract),
        authority_contract=contract,
    )
    request["clarification_resume_context"]["original_intent"][
        "baseline_candidates"
    ] = ["previous_day"]

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:baselines",
    ):
        workflow._bind_clarification_resume_intent(
            {},
            request,
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )


def test_material_authority_flattens_two_item_time_window_baseline_list():
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    route_baselines = [
        "rolling_7_day_baseline",
        "same_weekday_last_week",
    ]
    material = build_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        original_intent={
            "question_family": "custom_baseline_comparison",
            "question_families": ["custom_baseline_comparison"],
            "primary_question_family": "custom_baseline_comparison",
            "secondary_question_families": [],
            "target_metric": "paid_amount",
            "time_window": {
                "target": "yesterday",
                "baseline": [
                    {"type": "rolling_average", "window": 7},
                    {"ref": "last_week_same_day"},
                ],
            },
            "baseline_candidates": [],
            "requested_components": [],
            "requested_dimensions": [],
            "context_sources": [],
            "claim_intents": ["comparative_change"],
            "scope": "full_sample",
        },
        material_slots={
            **_complete_material_slots(
                baselines=route_baselines,
                claim_intents=["comparative_change"],
            ),
            "diagnostic_tags": [],
            "scope": "full_sample",
        },
    )

    assert material["intent_material"]["baselines"] == route_baselines


@pytest.mark.parametrize(
    ("narrative", "route_baseline"),
    [
        ("previous business day", "previous_day"),
        ("past 7 days", "rolling_7_day_baseline"),
        ("近7日", "rolling_7_day_baseline"),
        ("上周同期", "same_weekday_last_week"),
    ],
)
def test_material_authority_rejects_ambiguous_untyped_baseline_aliases(
    narrative, route_baseline
):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_baselines_invalid",
    ):
        build_material_authority(
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            original_intent={
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "primary_question_family": "custom_baseline_comparison",
                "secondary_question_families": [],
                "target_metric": "paid_amount",
                "time_window": "latest_complete_day",
                "baseline_candidates": [narrative],
                "requested_components": [],
                "requested_dimensions": [],
                "context_sources": [],
                "claim_intents": ["comparative_change"],
                "scope": "full_sample",
            },
            material_slots={
                **_complete_material_slots(
                    baselines=[route_baseline],
                    claim_intents=["comparative_change"],
                ),
                "diagnostic_tags": [],
                "scope": "full_sample",
            },
        )


@pytest.mark.parametrize(
    "narrative",
    [
        "前一天或近7日均值",
        "前一日和上周同日",
    ],
)
def test_material_authority_rejects_composite_previous_day_narratives(
    narrative,
):
    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_baselines_invalid",
    ):
        _signed_material_authority(
            {"baseline_candidates": [narrative]},
            {"baselines": ["previous_day"]},
        )


def test_material_authority_accepts_closed_previous_day_metric_suffix_label():
    material = _signed_material_authority(
        {"baseline_candidates": ["前日付费金额"]},
        {"baselines": ["previous_day"]},
    )

    assert material["intent_material"]["baselines"] == ["previous_day"]


@pytest.mark.parametrize(
    ("candidate", "route_baseline"),
    [
        (
            {
                "type": "rolling_average",
                "window": 30,
                "baseline_id": "rolling_7_day_baseline",
            },
            "rolling_7_day_baseline",
        ),
        (
            {
                "type": "same_weekday",
                "lag_weeks": 2,
                "ref": "last_week_same_day",
            },
            "same_weekday_last_week",
        ),
        (
            {
                "type": "same_weekday",
                "lag_weeks": 0,
                "ref": "last_week_same_day",
            },
            "same_weekday_last_week",
        ),
        (
            {
                "type": "custom_window",
                "window": 7,
                "baseline_id": "rolling_7_day_baseline",
            },
            "rolling_7_day_baseline",
        ),
        (
            {
                "type": 0,
                "baseline_id": "previous_day",
            },
            "previous_day",
        ),
    ],
)
def test_material_authority_rejects_typed_constraint_conflicts(
    candidate,
    route_baseline,
):
    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_baselines_invalid",
    ):
        _signed_material_authority(
            {"baseline_candidates": [candidate]},
            {"baselines": [route_baseline]},
        )


def test_material_authority_rejects_partially_overlapping_selected_candidates():
    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_baselines_invalid",
    ):
        _signed_material_authority(
            {
                "time_window": {
                    "target": "yesterday",
                    "baseline": [
                        "previous_day",
                        {"ref": "last_week_same_day"},
                    ],
                },
                "baseline_candidates": [
                    "previous_day",
                    {"type": "rolling_average", "window": 7},
                ],
            },
            {
                "baselines": [
                    "previous_day",
                    "rolling_7_day_baseline",
                    "same_weekday_last_week",
                ]
            },
        )


def test_material_authority_accepts_selected_subset_of_candidate_authority():
    material = _signed_material_authority(
        {
            "time_window": {
                "target": "yesterday",
                "baseline": "previous_day",
            },
            "baseline_candidates": [
                "previous_day",
                {"type": "rolling_average", "window": 7},
            ],
        },
        {
            "baselines": [
                "previous_day",
                "rolling_7_day_baseline",
            ]
        },
    )

    assert material["intent_material"]["baselines"] == [
        "previous_day",
        "rolling_7_day_baseline",
    ]


def test_terminal_and_nonterminal_baseline_canonicalization_have_parity():
    from bi_agent.runtime import langgraph_workflow as workflow

    candidates = [
        {"type": "rolling_average", "window": 7},
        {"ref": "last_week_same_day"},
    ]
    assert workflow._canonical_baseline_ids(candidates) == [
        "rolling_7_day_baseline",
        "same_weekday_last_week",
    ]

    with pytest.raises(
        workflow.WorkflowFailure,
        match="clarification_resume_material_slots_conflict:baselines",
    ):
        workflow._validate_nonterminal_resume_material(
            {
                "target_metric": "paid_amount",
                "baseline_candidates": ["past 7 days"],
                "requested_components": [],
                "requested_dimensions": [],
                "context_sources": [],
                "claim_intents": [],
            },
            _complete_material_slots(),
        )


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
        envelope["schema_version"] = "1"
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
        runtime_material=_runtime_material_for_contract(contract),
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
        runtime_material=_runtime_material_for_contract(contract),
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
    runtime_contract = deepcopy(contract)
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
        runtime_material=_runtime_material_for_contract(runtime_contract),
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_contract_target_metrics_unresolvable",
    ):
        _validate_signed_authority_pair(contract, material)


@pytest.mark.parametrize(
    "target_ref",
    [
        "contracts/metrics/paid-amount.metric.yaml@0.1",
        "contracts/sources/market-dashboard.source.yaml@0.1#field_contracts.paid_amount",
    ],
)
def test_completed_material_preflight_resolves_queryless_reviewed_target_ref(
    target_ref,
):
    from bi_agent.conversation.clarification_authority import (
        preflight_completed_material_authority,
    )

    contract = _source_contract()
    contract["target_metric_refs"] = [target_ref]
    contract["metric_bindings"] = []
    contract["contract_signature"] = analysis_contract_signature(contract)
    material = _signed_material_authority(
        runtime_material=_runtime_material_for_contract(contract),
    )
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )

    assert preflight_completed_material_authority(
        material_authority=material,
        analysis_contract=contract,
        run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        runtime_registry=registry,
    ) == ("paid_amount",)


@pytest.mark.parametrize(
    "target_ref",
    [
        "contracts/backlog/missing-contracts.yaml#component_contracts",
        "contracts/metrics/unknown.metric.yaml@0.1",
    ],
)
def test_completed_material_preflight_rejects_ambiguous_or_unknown_queryless_target(
    target_ref,
):
    from bi_agent.conversation.clarification_authority import (
        preflight_completed_material_authority,
    )

    contract = _source_contract()
    contract["target_metric_refs"] = [target_ref]
    contract["metric_bindings"] = []
    contract["contract_signature"] = analysis_contract_signature(contract)
    material = _signed_material_authority(
        runtime_material=_runtime_material_for_contract(contract),
    )
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^material_authority_contract_target_metrics_unresolvable$",
    ):
        preflight_completed_material_authority(
            material_authority=material,
            analysis_contract=contract,
            run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            runtime_registry=registry,
        )


def test_bound_contract_overlap_does_not_load_runtime_registry(monkeypatch):
    from bi_agent.conversation.clarification_authority import (
        validate_material_authority_contract_overlap,
    )
    from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict

    contract = _source_contract()
    material = _signed_material_authority(
        runtime_material=_runtime_material_for_contract(contract),
    )
    typed_contract = analysis_contract_from_dict(
        {
            key: value
            for key, value in contract.items()
            if key != "contract_signature"
        }
    )

    def fail_registry_load(*_args, **_kwargs):
        raise AssertionError("bound target must not load runtime registry")

    monkeypatch.setattr(RuntimeContractRegistry, "from_path", fail_registry_load)

    validate_material_authority_contract_overlap(material, typed_contract)


def test_queryless_completed_material_preflight_rejects_registry_digest_drift():
    from bi_agent.conversation.clarification_authority import (
        preflight_completed_material_authority,
    )

    contract = _source_contract()
    contract["target_metric_refs"] = [
        "contracts/metrics/paid-amount.metric.yaml@0.1"
    ]
    contract["metric_bindings"] = []
    contract["contract_signature"] = analysis_contract_signature(contract)
    material = _signed_material_authority(
        runtime_material=_runtime_material_for_contract(contract),
    )
    drifted_registry = deepcopy(
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
    )
    drifted_registry._source_payload_digest = "0" * 64

    with pytest.raises(
        EvidenceIntegrityError,
        match="^completed_material_authority_runtime_registry_mismatch$",
    ):
        preflight_completed_material_authority(
            material_authority=material,
            analysis_contract=contract,
            run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
            runtime_registry=drifted_registry,
        )


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
        runtime_material=_runtime_material_for_contract(_source_contract()),
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
        runtime_material=_runtime_material_for_contract(_source_contract()),
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_contract_scope_mismatch",
    ):
        _validate_signed_authority_pair(_source_contract(), material)


def _terminal_runtime_material():
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    return {
        "schema_version": "1",
        "target_semantic": "2026-06-02",
        "as_of": "2026-06-03T12:00:00+01:00",
        "business_timezone": "Africa/Lagos",
        "permission_scope": "analyst",
        "fixed_window_bounds": {
            "target_day": ["2026-06-02", "2026-06-02"],
            "previous_day": ["2026-06-01", "2026-06-01"],
            "rolling_7_day_baseline": ["2026-05-26", "2026-06-01"],
            "same_weekday_last_week": ["2026-05-26", "2026-05-26"],
            "pattern_history": ["2026-01-01", "2026-06-02"],
            "anomaly_history": ["2026-05-03", "2026-06-01"],
        },
        "filters": [],
        "grain": "window_id",
        "dataset_requirements": [],
        "metric_dataset_overrides": {},
        "dimension_dataset_overrides": {},
        "requested_context_sources": ["market_dashboard"],
        "accepted_graph": ["compare_periods"],
        "runtime_contract_version": registry.contract_version,
        "runtime_registry_digest": registry.source_payload_digest,
        "run_mode_class": "authoritative",
        "source_query_contracts": [],
    }


def _terminal_material_axis_authority():
    return _signed_material_authority(
        original_intent={
            "question_family": "business_object_impact_review",
            "question_families": ["business_object_impact_review"],
            "target_metric": "paid_amount",
            "requested_components": ["paid_users"],
            "requested_dimensions": ["channel"],
            "baseline_candidates": ["previous_day"],
            "context_sources": ["market_dashboard"],
            "claim_intents": ["comparative_change"],
            "scope": "full_sample",
            "time_window": {
                "target": "yesterday",
                "baseline": "previous_day",
            },
        },
        material_slots={
            "target_metrics": ["paid_amount"],
            "requested_components": ["paid_users"],
            "requested_dimensions": ["channel"],
            "baselines": ["previous_day", "rolling_7_day_baseline"],
            "context_sources": ["market_dashboard"],
            "claim_intents": ["comparative_change"],
            "diagnostic_tags": ["event_impact"],
            "scope": "full_sample",
        },
        runtime_material=_terminal_runtime_material(),
    )


def _exact_terminal_material_proposal():
    return {
        "question_families": ["business_object_impact_review"],
        "target_metrics": ["paid_amount"],
        "requested_components": ["paid_users"],
        "requested_dimensions": ["channel"],
        "baselines": ["previous_day", "rolling_7_day_baseline"],
        "context_sources": ["market_dashboard"],
        "requested_context_sources": ["market_dashboard"],
        "claim_intents": ["comparative_change"],
        "diagnostic_tags": ["event_impact"],
        "scope": "full_sample",
        "time_window": {
            "target": "yesterday",
            "baseline": "previous_day",
        },
        "target_semantic": "2026-06-02",
        "fixed_window_bounds": _terminal_runtime_material()[
            "fixed_window_bounds"
        ],
        "filters": [],
        "grain": "window_id",
        "dataset_requirements": [],
        "metric_dataset_overrides": {},
        "dimension_dataset_overrides": {},
    }


@pytest.mark.parametrize(
    ("axis", "drift", "reason_axis"),
    [
        ("question_families", ["pattern_explanation"], "question_families"),
        ("target_metrics", ["active_users"], "target_metrics"),
        ("requested_components", ["paid_orders"], "requested_components"),
        ("requested_dimensions", ["game"], "requested_dimensions"),
        ("baselines", ["same_weekday_last_week"], "baselines"),
        ("context_sources", ["external_event"], "context_sources"),
        (
            "requested_context_sources",
            ["external_event"],
            "context_sources",
        ),
        ("claim_intents", ["candidate_mechanism"], "claim_intents"),
        ("diagnostic_tags", ["anomaly"], "diagnostic_tags"),
        ("scope", "custom_segment", "scope"),
        (
            "time_window",
            {"target": "today", "baseline": "previous_day"},
            "time_window",
        ),
        ("target_semantic", "today", "time_window"),
        (
            "fixed_window_bounds",
            {"target_day": ["2027-01-01", "2027-01-01"]},
            "fixed_window_bounds",
        ),
    ],
)
def test_terminal_resume_proposal_rejects_every_signed_material_axis_drift(
    axis, drift, reason_axis
):
    from bi_agent.conversation.clarification_authority import (
        validate_terminal_resume_proposal_overlap,
    )

    proposal = _exact_terminal_material_proposal()
    proposal[axis] = drift

    with pytest.raises(
        EvidenceIntegrityError,
        match=f"terminal_resume_proposal_{reason_axis}_mismatch",
    ):
        validate_terminal_resume_proposal_overlap(
            _terminal_material_axis_authority(),
            proposal,
        )


@pytest.mark.parametrize(
    ("choice", "reason_axis"),
    [
        ({"requested_components": ["paid_orders"]}, "requested_components"),
        ({"requested_dimensions": ["game"]}, "requested_dimensions"),
        ({"baselines": ["same_weekday_last_week"]}, "baselines"),
        ({"baseline_candidates": ["same_weekday_last_week"]}, "baselines"),
        ({"context_sources": ["external_event"]}, "context_sources"),
        (
            {"requested_context_sources": ["external_event"]},
            "context_sources",
        ),
        ({"claim_intents": ["candidate_mechanism"]}, "claim_intents"),
        ({"diagnostic_tags": ["anomaly"]}, "diagnostic_tags"),
        (
            {"time_window": {"target": "today"}},
            "time_window",
        ),
        ({"target_semantic": "today"}, "time_window"),
        ({"target_window": "today"}, "time_window"),
    ],
)
def test_terminal_resume_choice_rejects_every_signed_material_axis_drift(
    choice, reason_axis
):
    from bi_agent.conversation.clarification_authority import (
        validate_terminal_clarification_choice_overlap,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match=f"terminal_resume_proposal_{reason_axis}_mismatch",
    ):
        validate_terminal_clarification_choice_overlap(
            _terminal_material_axis_authority(),
            choice,
        )


def test_terminal_resume_rebuilds_runtime_material_from_signed_route_authority():
    from bi_agent.conversation.clarification_authority import (
        bind_terminal_resume_proposal_material,
    )

    bound = bind_terminal_resume_proposal_material(
        _terminal_material_axis_authority(),
        {
            "accepted_degradation_choice": {"choice_id": "continue"},
            "non_material_suggestion": "explain limitations first",
        },
    )

    assert bound == {
        "accepted_degradation_choice": {"choice_id": "continue"},
        "non_material_suggestion": "explain limitations first",
        **_exact_terminal_material_proposal(),
    }


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
        "baseline_candidates": ["上周同日"],
        "context_sources": [],
        "claim_intents": [],
        "requested_dimensions": [],
        "requested_components": [],
        "question": "source question",
    }
    material = _complete_material_slots(
        baselines=["same_weekday_last_week", "previous_day"],
        claim_intents=["comparative_change"],
    )
    contract = _source_contract_with_window(
        "same_weekday_last_week", role="baseline"
    )
    previous = deepcopy(contract["resolved_windows"][-1])
    previous["window_id"] = "previous_day"
    contract["resolved_windows"].append(previous)
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


def _runtime_material_for_contract(
    contract,
    *,
    proposal=None,
    accepted_graph=(),
    query_contracts=(),
    capability_execution_plans=(),
):
    from bi_agent.conversation.clarification_authority import (
        build_execution_material,
    )

    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    return build_execution_material(
        proposal=proposal or {},
        accepted_graph=accepted_graph,
        as_of=contract["as_of"],
        permission_scope=contract["permission_scope"],
        run_mode="production",
        runtime_contract_version=registry.contract_version,
        runtime_registry_digest=registry.source_payload_digest,
        analysis_contract=contract,
        query_contracts=query_contracts,
        capability_execution_plans=capability_execution_plans,
    )


def _material_query_contract(query_id, query_intent, snapshot_ref):
    from bi_agent.runtime.analysis_contracts import query_contract_signature

    contract = {
        "query_contract_id": query_id,
        "analysis_contract_ref": "analysis:run-source:1",
        "query_intent": query_intent,
        "dataset_snapshot_refs": [snapshot_ref],
        "metric_bindings": [],
        "dimension_bindings": [],
        "window_refs": ["target_day"],
        "resolved_windows": [],
        "filters": [],
        "result_shape": {
            "required_fields": ["window_id", "value"],
            "unique_key": ["window_id"],
            "grain": ["window_id"],
            "required_window_ids": ["target_day"],
            "result_semantics": "complete_aggregate",
            "dimension_presence_policy": "paired_required",
        },
        "completeness_assertions": ["target_window_present"],
        "permission_scope": "analyst",
        "workload_class": "interactive",
        "query_parameters": {},
        "query_role_ref": "",
        "reconciliation_binding": None,
        "join_expectation": None,
    }
    contract["contract_signature"] = query_contract_signature(contract)
    return contract


def _material_capability_plan(capability_id, *query_contract_refs):
    return {
        "capability_id": capability_id,
        "required_input_slots": [
            {
                "query_contract_refs": list(query_contract_refs),
                "validation_query_contract_refs": [],
            }
        ],
        "optional_input_slots": [],
    }


@pytest.mark.parametrize(
    ("accepted_graph", "capability_execution_plans"),
    [
        (
            ("compare_periods",),
            (
                _material_capability_plan(
                    "compare_periods", "query:source:unknown"
                ),
            ),
        ),
        (
            ("compare_periods",),
            (_material_capability_plan("compare_periods"),),
        ),
        (
            ("compare_periods",),
            (
                _material_capability_plan(
                    "answer_verify", "query:source:owned"
                ),
            ),
        ),
    ],
    ids=(
        "unknown-plan-query-ref",
        "ownerless-source-query",
        "plan-owner-outside-accepted-graph",
    ),
)
def test_execution_material_rejects_invalid_source_query_owner_projection(
    accepted_graph, capability_execution_plans
):
    """Post-fix characterization: the prior schema did not read plan ownership."""
    source_query = _material_query_contract(
        "query:source:owned",
        "compare_periods",
        "snapshot:paid:source",
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="execution_material_source_query_contracts_invalid",
    ):
        _runtime_material_for_contract(
            _source_contract(),
            accepted_graph=accepted_graph,
            query_contracts=(source_query,),
            capability_execution_plans=capability_execution_plans,
        )


def _two_query_material_authority():
    query_contracts = (
        _material_query_contract(
            "query:source:compare",
            "compare_periods",
            "snapshot:paid:source",
        ),
        _material_query_contract(
            "query:source:verify",
            "verify_answer_inputs",
            "snapshot:paid:source",
        ),
    )
    runtime_material = _runtime_material_for_contract(
        _source_contract(),
        accepted_graph=("compare_periods", "answer_verify"),
        query_contracts=query_contracts,
        capability_execution_plans=(
            _material_capability_plan(
                "compare_periods", "query:source:compare"
            ),
            _material_capability_plan(
                "answer_verify", "query:source:verify"
            ),
        ),
    )
    return _signed_material_authority(runtime_material=runtime_material), query_contracts


def test_terminal_compile_rejects_deleting_all_signed_source_queries():
    from bi_agent.conversation.clarification_authority import (
        validate_terminal_compile_overlap,
    )

    authority, _ = _two_query_material_authority()

    with pytest.raises(
        EvidenceIntegrityError,
        match="terminal_resume_compile_query_contract_projection_mismatch",
    ):
        validate_terminal_compile_overlap(
            authority,
            analysis_contract=_source_contract(),
            query_contracts=(),
            accepted_graph=("compare_periods", "answer_verify"),
            accepted_choice={"affected_capabilities": []},
        )


def test_terminal_compile_rejects_deleting_one_signed_source_query():
    from bi_agent.conversation.clarification_authority import (
        validate_terminal_compile_overlap,
    )

    authority, query_contracts = _two_query_material_authority()

    with pytest.raises(
        EvidenceIntegrityError,
        match="terminal_resume_compile_query_contract_projection_mismatch",
    ):
        validate_terminal_compile_overlap(
            authority,
            analysis_contract=_source_contract(),
            query_contracts=query_contracts[:1],
            accepted_graph=("compare_periods", "answer_verify"),
            accepted_choice={"affected_capabilities": []},
        )


def test_terminal_compile_accepts_complete_signed_source_query_projection():
    from bi_agent.conversation.clarification_authority import (
        validate_terminal_compile_overlap,
    )

    authority, query_contracts = _two_query_material_authority()

    validate_terminal_compile_overlap(
        authority,
        analysis_contract=_source_contract(),
        query_contracts=query_contracts,
        accepted_graph=("compare_periods", "answer_verify"),
        accepted_choice={"affected_capabilities": []},
    )


def test_terminal_compile_allows_query_deletion_for_legally_removed_owner():
    from bi_agent.conversation.clarification_authority import (
        validate_terminal_compile_overlap,
    )

    authority, query_contracts = _two_query_material_authority()

    validate_terminal_compile_overlap(
        authority,
        analysis_contract=_source_contract(),
        query_contracts=query_contracts[:1],
        accepted_graph=("compare_periods",),
        accepted_choice={"affected_capabilities": ["answer_verify"]},
    )


def test_terminal_compile_keeps_shared_query_when_one_owner_remains():
    from bi_agent.conversation.clarification_authority import (
        validate_terminal_compile_overlap,
    )

    shared_query = _material_query_contract(
        "query:source:shared",
        "shared_observation",
        "snapshot:paid:source",
    )
    runtime_material = _runtime_material_for_contract(
        _source_contract(),
        accepted_graph=("compare_periods", "answer_verify"),
        query_contracts=(shared_query,),
        capability_execution_plans=(
            _material_capability_plan(
                "compare_periods", "query:source:shared"
            ),
            _material_capability_plan(
                "answer_verify", "query:source:shared"
            ),
        ),
    )
    authority = _signed_material_authority(
        runtime_material=runtime_material
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="terminal_resume_compile_query_contract_projection_mismatch",
    ):
        validate_terminal_compile_overlap(
            authority,
            analysis_contract=_source_contract(),
            query_contracts=(),
            accepted_graph=("compare_periods",),
            accepted_choice={"affected_capabilities": ["answer_verify"]},
        )


@pytest.mark.parametrize(
    "axis",
    ["dataset_requirements", "requested_context_sources"],
)
@pytest.mark.parametrize(
    "mapping_value",
    [
        {"paid_order_success": True},
        MappingProxyType({"paid_order_success": True}),
    ],
)
def test_execution_material_sequence_axes_reject_mapping_values(
    axis, mapping_value
):
    reason = f"execution_material_{axis}_invalid"

    with pytest.raises(EvidenceIntegrityError, match=reason):
        _runtime_material_for_contract(
            _source_contract(),
            proposal={axis: mapping_value},
        )


def _signed_material_authority(
    original_intent=None,
    material_slots=None,
    *,
    source_run_id="run-source",
    thread_id="thread-source",
    topic_id="topic-source",
    obligation_rejection_history=(),
    runtime_material=None,
):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )

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
    original.update(
        {
            "question_family": primary_family,
            "question_families": families,
            "primary_question_family": primary_family,
            "secondary_question_families": families[1:],
            "target_metric": primary_target,
            "requested_components": list(
                original.get("requested_components") or ()
            ),
            "requested_dimensions": list(
                original.get("requested_dimensions") or ()
            ),
            "baseline_candidates": list(
                original.get("baseline_candidates") or ()
            ),
            "context_sources": list(original.get("context_sources") or ()),
            "claim_intents": list(original.get("claim_intents") or ()),
            "scope": original.get("scope", "full_sample"),
        }
    )
    if runtime_material is None:
        runtime_material = _runtime_material_for_contract(_source_contract())
    return build_material_authority(
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        original_intent=original,
        material_slots=slots,
        runtime_material=runtime_material,
        obligation_rejection_history=obligation_rejection_history,
    )


def _completed_material_authority_record(
    *,
    contract,
    material_authority,
    source_run_id="run-source",
    thread_id="thread-1",
    topic_id="topic-1",
):
    from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value

    body = {
        "schema_version": "completed-material-authority.v1",
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "analysis_contract_ref": contract["analysis_contract_id"],
        "analysis_contract_signature": contract["contract_signature"],
        "analysis_contract_digest": canonical_digest(contract),
        "material_authority": canonical_value(material_authority),
        "material_authority_digest": canonical_digest(material_authority),
    }
    return {**body, "record_digest": canonical_digest(body)}


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
            runtime_material=_runtime_material_for_contract(
                authority_contract,
                accepted_graph=authority_contract.get(
                    "capability_requirements", ()
                ),
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


def test_memory_completed_followup_authority_resolves_signed_material_and_contract():
    store, contract = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    store.finalize_completed_material_authority(
        run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        request={"question": "source question"},
        material_authority=material_authority,
    )

    authority = store.resolve_completed_material_authority(
        source_run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
    )

    assert authority["source_run_id"] == "run-source"
    assert authority["thread_id"] == "thread-1"
    assert authority["topic_id"] == "topic-1"
    assert authority["analysis_contract_signature"] == contract["contract_signature"]
    assert authority["material_authority"] == store.runs["run-source"][
        "request"
    ]["material_authority"]


@pytest.mark.parametrize("status", ["failed", "waiting_for_clarification"])
def test_memory_completed_followup_authority_rejects_noncompleted_source(status):
    store, contract = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    store.finalize_completed_material_authority(
        run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        request={"question": "source question"},
        material_authority=material_authority,
    )
    store.runs["run-source"]["status"] = status

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_source_run_not_complete",
    ):
        store.resolve_completed_material_authority(
            source_run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
        )


def test_completed_followup_authority_rejects_resigned_request_material_drift():
    from bi_agent.runtime.analysis_contracts import stable_contract_signature

    store, _ = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    store.finalize_completed_material_authority(
        run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        request={"question": "source question"},
        material_authority=material_authority,
    )
    mutable = store.runs["run-source"]["request"]["material_authority"]
    mutable["route_material_slots"]["diagnostic_tags"] = ["baseline_stability"]
    body = {
        key: value
        for key, value in mutable.items()
        if key != "material_authority_signature"
    }
    mutable["material_authority_signature"] = stable_contract_signature(body)

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_authority_record_mismatch",
    ):
        store.resolve_completed_material_authority(
            source_run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
        )


def test_completed_followup_authority_finalization_is_exactly_idempotent():
    store, _ = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    kwargs = {
        "run_id": "run-source",
        "thread_id": "thread-1",
        "topic_id": "topic-1",
        "request": {"question": "source question"},
        "material_authority": material_authority,
    }

    store.finalize_completed_material_authority(**kwargs)
    store.finalize_completed_material_authority(**kwargs)

    events = [
        event
        for event in store.audit_events
        if event["event_type"] == "completed_material_authority_recorded"
        and event["run_id"] == "run-source"
    ]
    assert len(events) == 1


def test_completed_followup_authority_cannot_be_backfilled_for_historical_completed_run():
    store, _ = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "completed"

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_source_run_not_finalizable",
    ):
        store.finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={"question": "source question"},
            material_authority=material_authority,
        )

    assert not any(
        event["event_type"] == "completed_material_authority_recorded"
        for event in store.audit_events
    )


def test_completed_followup_authority_finalization_records_status_transition_once():
    store, _ = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    before = sum(
        event["event_type"] == "run_status_changed"
        for event in store.audit_events
    )
    kwargs = {
        "run_id": "run-source",
        "thread_id": "thread-1",
        "topic_id": "topic-1",
        "request": {"question": "source question"},
        "material_authority": material_authority,
    }

    store.finalize_completed_material_authority(**kwargs)
    store.finalize_completed_material_authority(**kwargs)

    completed_status_events = [
        event
        for event in store.audit_events
        if event["event_type"] == "run_status_changed"
        and event["run_id"] == "run-source"
        and event.get("payload", {}).get("status") == "completed"
    ]
    assert len(completed_status_events) == 1
    assert sum(
        event["event_type"] == "run_status_changed"
        for event in store.audit_events
    ) == before + 1


def test_completed_followup_authority_exact_replay_rejects_event_owner_ref_drift():
    store, _ = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    kwargs = {
        "run_id": "run-source",
        "thread_id": "thread-1",
        "topic_id": "topic-1",
        "request": {"question": "source question"},
        "material_authority": material_authority,
    }
    store.finalize_completed_material_authority(**kwargs)
    event = next(
        item
        for item in store._audit_events
        if item["event_type"] == "completed_material_authority_recorded"
    )
    event["ref"] = "completed-material-authority:run-other"

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_authority_record_conflict",
    ):
        store.finalize_completed_material_authority(**kwargs)
    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_authority_record_mismatch",
    ):
        store.resolve_completed_material_authority(
            source_run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
        )


def test_completed_followup_authority_inmemory_finalization_is_atomic_on_audit_failure():
    class FailSecondStagedAuditStore(InMemoryConversationStore):
        def __init__(self):
            super().__init__()
            self.staged_audit_count = 0

        def _append_staged_audit_event(self, events, event):
            self.staged_audit_count += 1
            if self.staged_audit_count == 2:
                raise RuntimeError("injected staged audit failure")
            return super()._append_staged_audit_event(events, event)

    base, contract = _seed_memory_store()
    store = FailSecondStagedAuditStore()
    store.__dict__.update(deepcopy(base.__dict__))
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    before_run = deepcopy(store.runs["run-source"])
    before_events = store.audit_events

    with pytest.raises(RuntimeError, match="injected staged audit failure"):
        store.finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={"question": "source question"},
            material_authority=material_authority,
        )

    assert store.runs["run-source"] == before_run
    assert store.audit_events == before_events


def test_completed_followup_authority_resolver_rejects_duplicate_events():
    store, _ = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    store.finalize_completed_material_authority(
        run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        request={"question": "source question"},
        material_authority=material_authority,
    )
    event = next(
        item
        for item in store.audit_events
        if item["event_type"] == "completed_material_authority_recorded"
    )
    store.add_audit_event(
        event["event_type"],
        thread_id=event["thread_id"],
        topic_id=event["topic_id"],
        run_id=event["run_id"],
        ref=event["ref"],
        payload=event["payload"],
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_authority_record_ambiguous",
    ):
        store.resolve_completed_material_authority(
            source_run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
        )


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


def test_postgres_completed_followup_authority_matches_inmemory_contract():
    contract = _source_contract()
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    authority_record = _completed_material_authority_record(
        contract=contract,
        material_authority=material_authority,
    )
    connection = FakeConnection(rows=[{
        "analysis_contract_id": contract["analysis_contract_id"],
        "analysis_run_id": "run-source",
        "stored_contract_signature": contract["contract_signature"],
        "contract_payload": json.dumps(contract),
        "run_status": "completed",
        "run_thread_id": "thread-1",
        "run_topic_id": "topic-1",
        "run_request": json.dumps({
            "analysis_contract": contract,
            "material_authority": material_authority,
        }),
        "authority_record_payload": json.dumps(authority_record),
        "authority_record_ref": "completed-material-authority:run-source",
        "authority_event_run_id": "run-source",
        "authority_event_thread_id": "thread-1",
        "authority_event_topic_id": "topic-1",
    }])

    resolved = PostgresConversationStore(
        connection
    ).resolve_completed_material_authority(
        source_run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
    )

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "waje_runtime.analysis_runs" in sql
    assert "waje_runtime.analysis_contracts" in sql
    assert "waje_runtime.audit_events" in sql
    assert resolved["analysis_contract_signature"] == contract["contract_signature"]
    assert resolved["material_authority"] == material_authority


def test_postgres_completed_followup_authority_resolver_rejects_missing_embedded_signature():
    contract = _source_contract()
    persisted_contract = {
        key: value
        for key, value in contract.items()
        if key != "contract_signature"
    }
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    authority_record = _completed_material_authority_record(
        contract=contract,
        material_authority=material_authority,
    )
    connection = FakeConnection(rows=[{
        "analysis_contract_id": contract["analysis_contract_id"],
        "analysis_run_id": "run-source",
        "stored_contract_signature": contract["contract_signature"],
        "contract_payload": json.dumps(persisted_contract),
        "run_status": "completed",
        "run_thread_id": "thread-1",
        "run_topic_id": "topic-1",
        "run_request": json.dumps({
            "analysis_contract": contract,
            "material_authority": material_authority,
        }),
        "authority_record_payload": json.dumps(authority_record),
        "authority_record_ref": "completed-material-authority:run-source",
        "authority_event_run_id": "run-source",
        "authority_event_thread_id": "thread-1",
        "authority_event_topic_id": "topic-1",
    }])

    with pytest.raises(
        EvidenceIntegrityError,
        match="^completed_followup_contract_signature_invalid$",
    ):
        PostgresConversationStore(
            connection
        ).resolve_completed_material_authority(
            source_run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
        )


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "completed_followup_source_run_missing"),
        (
            [
                {
                    "analysis_contract_id": None,
                    "analysis_run_id": None,
                    "stored_contract_signature": None,
                    "contract_payload": None,
                    "run_status": "completed",
                    "run_thread_id": "thread-1",
                    "run_topic_id": "topic-1",
                    "run_request": json.dumps({}),
                    "authority_record_payload": None,
                    "authority_record_ref": None,
                    "authority_event_run_id": None,
                    "authority_event_thread_id": None,
                    "authority_event_topic_id": None,
                }
            ],
            "completed_followup_contract_missing",
        ),
        (
            [
                {
                    "analysis_contract_id": _source_contract()[
                        "analysis_contract_id"
                    ],
                    "analysis_run_id": "run-source",
                    "stored_contract_signature": _source_contract()[
                        "contract_signature"
                    ],
                    "contract_payload": json.dumps(_source_contract()),
                    "run_status": "completed",
                    "run_thread_id": "thread-1",
                    "run_topic_id": "topic-1",
                    "run_request": json.dumps({}),
                    "authority_record_payload": None,
                    "authority_record_ref": None,
                    "authority_event_run_id": None,
                    "authority_event_thread_id": None,
                    "authority_event_topic_id": None,
                }
            ],
            "completed_followup_authority_record_missing",
        ),
    ],
)
def test_postgres_completed_followup_authority_missing_reason_matches_inmemory(
    rows,
    reason,
):
    with pytest.raises(EvidenceIntegrityError, match=reason):
        PostgresConversationStore(
            FakeConnection(rows=rows)
        ).resolve_completed_material_authority(
            source_run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
        )


class _CompletedFinalizationCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _CompletedFinalizationConnection:
    def __init__(self, *, run_row, authority_events=()):
        self.run_row = run_row
        self.authority_events = list(authority_events)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))
        if (
            "completed_material_authority_finalization_run_lock" in statement
            or "completed_material_authority_finalization_contract_lock"
            in statement
        ):
            return _CompletedFinalizationCursor([self.run_row])
        if "completed_material_authority_existing_events" in statement:
            return _CompletedFinalizationCursor(self.authority_events)
        return _CompletedFinalizationCursor(())

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_conversation_stores_expose_write_failure_recovery_contract():
    connection = _CompletedFinalizationConnection(run_row={})

    assert InMemoryConversationStore().recover_after_write_failure() is None
    assert (
        PostgresConversationStore(connection).recover_after_write_failure()
        is None
    )
    assert connection.rollbacks == 1


def test_postgres_completed_followup_authority_finalizes_status_and_events_atomically():
    from bi_agent.runtime.evidence_authority import canonical_value

    contract = _source_contract()
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    connection = _CompletedFinalizationConnection(
        run_row={
            "run_status": "running_workflow",
            "run_thread_id": "thread-1",
            "run_topic_id": "topic-1",
            "run_request": json.dumps({"question": "source question"}),
            "analysis_run_id": "run-source",
            "stored_contract_signature": contract["contract_signature"],
            "contract_payload": json.dumps(contract),
        }
    )

    finalized = PostgresConversationStore(
        connection
    ).finalize_completed_material_authority(
        run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        request={"question": "source question"},
        material_authority=material_authority,
    )

    assert finalized["analysis_contract"] == canonical_value(contract)
    assert finalized["material_authority"] == canonical_value(material_authority)
    assert connection.commits == 1
    assert connection.rollbacks == 0
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert sql.count("FOR UPDATE") == 3
    assert "completed_material_authority_finalization_run_lock" in sql
    assert "completed_material_authority_finalization_contract_lock" in sql
    assert "UPDATE waje_runtime.analysis_runs" in sql
    event_params = [
        params
        for statement, params in connection.statements
        if "INSERT INTO waje_runtime.audit_events" in statement
    ]
    assert [params["event_type"] for params in event_params] == [
        "run_status_changed",
        "completed_material_authority_recorded",
    ]
    assert json.loads(event_params[0]["payload"]) == {"status": "completed"}


def test_postgres_completed_followup_authority_rejects_embedded_contract_signature_drift():
    contract = _source_contract()
    persisted_contract = {**contract, "contract_signature": "tampered-embedded"}
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    connection = _CompletedFinalizationConnection(
        run_row={
            "run_status": "running_workflow",
            "run_thread_id": "thread-1",
            "run_topic_id": "topic-1",
            "run_request": json.dumps({}),
            "analysis_run_id": "run-source",
            "stored_contract_signature": contract["contract_signature"],
            "contract_payload": json.dumps(persisted_contract),
        }
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_contract_signature_invalid",
    ):
        PostgresConversationStore(
            connection
        ).finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={},
            material_authority=material_authority,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_postgres_completed_followup_authority_rejects_missing_embedded_contract_signature():
    contract = _source_contract()
    persisted_contract = {
        key: value
        for key, value in contract.items()
        if key != "contract_signature"
    }
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    connection = _CompletedFinalizationConnection(
        run_row={
            "run_status": "running_workflow",
            "run_thread_id": "thread-1",
            "run_topic_id": "topic-1",
            "run_request": json.dumps({}),
            "analysis_run_id": "run-source",
            "stored_contract_signature": contract["contract_signature"],
            "contract_payload": json.dumps(persisted_contract),
        }
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^completed_followup_contract_signature_invalid$",
    ):
        PostgresConversationStore(
            connection
        ).finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={},
            material_authority=material_authority,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def _completed_replay_connection(*, duplicate_events=False):
    from bi_agent.runtime.evidence_authority import canonical_value

    contract = _source_contract()
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    finalized_request = canonical_value(
        {
            "question": "source question",
            "analysis_contract": contract,
            "material_authority": material_authority,
        }
    )
    record = _completed_material_authority_record(
        contract=contract,
        material_authority=material_authority,
    )
    event = {
        "payload": json.dumps(record),
        "ref": "completed-material-authority:run-source",
        "run_id": "run-source",
        "thread_id": "thread-1",
        "topic_id": "topic-1",
    }
    connection = _CompletedFinalizationConnection(
        run_row={
            "run_status": "completed",
            "run_thread_id": "thread-1",
            "run_topic_id": "topic-1",
            "run_request": json.dumps(finalized_request),
            "analysis_run_id": "run-source",
            "stored_contract_signature": contract["contract_signature"],
            "contract_payload": json.dumps(contract),
        },
        authority_events=(event, event) if duplicate_events else (event,),
    )
    return connection, material_authority, finalized_request


def test_postgres_completed_followup_authority_exact_replay_is_idempotent():
    connection, material_authority, finalized_request = (
        _completed_replay_connection()
    )
    store = PostgresConversationStore(connection)
    kwargs = {
        "run_id": "run-source",
        "thread_id": "thread-1",
        "topic_id": "topic-1",
        "request": {"question": "source question"},
        "material_authority": material_authority,
    }

    assert store.finalize_completed_material_authority(**kwargs) == finalized_request
    assert store.finalize_completed_material_authority(**kwargs) == finalized_request
    assert connection.commits == 0
    assert connection.rollbacks == 2


def test_postgres_completed_followup_authority_duplicate_event_conflicts():
    connection, material_authority, _ = _completed_replay_connection(
        duplicate_events=True
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^completed_followup_authority_record_conflict$",
    ):
        PostgresConversationStore(
            connection
        ).finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={"question": "source question"},
            material_authority=material_authority,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_postgres_completed_followup_authority_second_audit_failure_rolls_back_all():
    class SecondAuditFailureConnection(_CompletedFinalizationConnection):
        def __init__(self, *, run_row):
            super().__init__(run_row=run_row)
            self.audit_writes = 0

        def execute(self, statement, params=None):
            if "INSERT INTO waje_runtime.audit_events" in statement:
                self.audit_writes += 1
                if self.audit_writes == 2:
                    raise RuntimeError("injected second audit failure")
            return super().execute(statement, params)

    contract = _source_contract()
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    connection = SecondAuditFailureConnection(
        run_row={
            "run_status": "running_workflow",
            "run_thread_id": "thread-1",
            "run_topic_id": "topic-1",
            "run_request": json.dumps({}),
            "analysis_run_id": "run-source",
            "stored_contract_signature": contract["contract_signature"],
            "contract_payload": json.dumps(contract),
        }
    )

    with pytest.raises(RuntimeError, match="injected second audit failure"):
        PostgresConversationStore(
            connection
        ).finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={},
            material_authority=material_authority,
        )

    assert connection.audit_writes == 2
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_postgres_completed_followup_authority_finalizer_reports_missing_contract():
    material_authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    connection = _CompletedFinalizationConnection(
        run_row={
            "run_status": "running_workflow",
            "run_thread_id": "thread-1",
            "run_topic_id": "topic-1",
            "run_request": json.dumps({}),
            "analysis_run_id": None,
            "stored_contract_signature": None,
            "contract_payload": None,
        }
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_contract_missing",
    ):
        PostgresConversationStore(
            connection
        ).finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={},
            material_authority=material_authority,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


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
    from bi_agent.conversation.clarification_authority import (
        build_execution_material,
    )

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
            registry = RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            )
            execution_material = build_execution_material(
                proposal={
                    "question_families": [
                        "segment_or_factor_attribution"
                    ],
                    "target_metrics": ["paid_amount"],
                    "requested_dimensions": ["channel"],
                    "claim_intents": [
                        "segment_contribution_or_mix_shift"
                    ],
                },
                accepted_graph=("segment_contribution",),
                as_of=records["analysis_contract"]["as_of"],
                permission_scope="analyst",
                run_mode="production",
                runtime_contract_version=registry.contract_version,
                runtime_registry_digest=registry.source_payload_digest,
                analysis_contract=records["analysis_contract"],
                query_contracts=records["query_contracts"],
                capability_execution_plans=tuple(
                    binding.plan_payload
                    for binding in records["capability_binding_records"]
                ),
            )
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["segment_contribution"],
                    "analysis_contract": records["analysis_contract"],
                    "execution_material": execution_material,
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
    assert persisted_material_authority["schema_version"] == "3"
    assert persisted_material_authority["execution_material"][
        "target_semantic"
    ] == "2026-06-02"
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
    material_authority = _signed_material_authority(
        original_intent,
        material_slots,
        source_run_id="run-segment-source",
        thread_id="thread-segment-closure",
        topic_id=topic.topic_id,
        runtime_material=_runtime_material_for_contract(
            contract,
            proposal={
                "requested_context_sources": ["gameplay"],
            },
            accepted_graph=contract["capability_requirements"],
        ),
    )
    clarification = {
        "questions": [{
            "question": "缺口怎么处理？",
            "options": ["保留边界继续", "等待来源"],
        }],
        "recommended_assumption": {"option": "保留边界继续"},
        "choice_actions": [choice_action],
    }
    store.upsert_run(
        "run-segment-source",
        thread_id="thread-segment-closure",
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "material_authority": material_authority,
            "clarification_source_envelope": (
                _build_clarification_source_envelope(
                    source_run_id="run-segment-source",
                    source_thread_id="thread-segment-closure",
                    source_topic_id=topic.topic_id,
                    source_owner_id="user",
                    question="昨天渠道贡献如何？",
                    analysis_context={},
                    accepted_graph=contract["capability_requirements"],
                    analysis_contract=contract,
                    original_intent=original_intent,
                    material_slots=material_slots,
                    clarification=clarification,
                )
            ),
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
    clarification = {
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
    }
    store.upsert_run(
        "run-resume-reject-source",
        thread_id="thread-resume-reject",
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "clarification_source_envelope": (
                _build_clarification_source_envelope(
                    source_run_id="run-resume-reject-source",
                    source_thread_id="thread-resume-reject",
                    source_topic_id=topic.topic_id,
                    source_owner_id="user",
                    question="昨天付费如何？",
                    analysis_context={},
                    clarification=clarification,
                )
            ),
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
