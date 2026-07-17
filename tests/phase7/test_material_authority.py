from copy import deepcopy
from datetime import datetime
import json

import pytest

from bi_agent.conversation.clarification_authority import (
    _compiled_goal_material_projection,
    build_execution_material,
    build_material_authority,
    validate_material_authority,
)
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.analysis_contracts import analysis_contract_signature
from bi_agent.runtime.dataset_catalog import DatasetCatalog
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_value,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


_AS_OF = "2026-06-03T12:00:00+01:00"
_GOAL_BINDINGS = [{"goal_id": "explain_change", "role": "primary"}]
_EXPLICIT_FOCUS = {
    "component_ids": [],
    "dimension_ids": [],
    "context_source_ids": [],
}


def _goal_material():
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    return registry, _compiled_goal_material_projection(
        goal_bindings=_GOAL_BINDINGS,
        target_metric="paid_amount",
        explicit_focus=_EXPLICIT_FOCUS,
        runtime_registry=registry,
    )


def _source_contract(run_id="run-source"):
    registry, goal_material = _goal_material()
    proposal = {
        "question_families": ["business_object_impact_review"],
        "target_metrics": ["paid_amount"],
        "requested_components": goal_material["component_ids"],
        "association_metrics": goal_material["association_metric_ids"],
        "requested_dimensions": goal_material["dimension_ids"],
        "requested_context_sources": goal_material["context_sources"],
        "claim_intents": goal_material["claim_types"],
        "goal_bindings": _GOAL_BINDINGS,
        "explicit_focus": _EXPLICIT_FOCUS,
    }
    outcome = compile_analysis_contract(
        run_id=run_id,
        proposal=proposal,
        accepted_capabilities=("compare_periods", "answer_verify"),
        catalog=DatasetCatalog(()),
        registry=registry,
        as_of=datetime.fromisoformat(_AS_OF),
    )
    payload = outcome.analysis_contract.to_dict()
    payload["contract_signature"] = analysis_contract_signature(payload)
    return payload


def _runtime_material_for_contract(
    contract,
    *,
    proposal=None,
    accepted_graph=(),
    query_contracts=(),
    capability_execution_plans=(),
):
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    return build_execution_material(
        proposal=proposal or {},
        accepted_graph=accepted_graph,
        as_of=contract["as_of"],
        run_mode="production",
        runtime_contract_version=registry.contract_version,
        runtime_registry_digest=registry.source_payload_digest,
        analysis_contract=contract,
        query_contracts=query_contracts,
        capability_execution_plans=capability_execution_plans,
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
    _, goal_material = _goal_material()
    original = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "goal_bindings": deepcopy(_GOAL_BINDINGS),
        "explicit_focus": deepcopy(_EXPLICIT_FOCUS),
        "baseline_candidates": [],
        "scope": "full_sample",
        "time_window": {"target": "2026-06-02"},
        **deepcopy(original_intent or {}),
    }
    slots = {
        "target_metrics": ["paid_amount"],
        "component_ids": list(goal_material["component_ids"]),
        "association_metric_ids": list(
            goal_material["association_metric_ids"]
        ),
        "dimension_ids": list(goal_material["dimension_ids"]),
        "baselines": [],
        "context_sources": list(goal_material["context_sources"]),
        "claim_types": list(goal_material["claim_types"]),
        "required_outcomes": list(goal_material["required_outcomes"]),
        "analysis_axis_ids": list(goal_material["analysis_axis_ids"]),
        "diagnostic_tags": [],
        "scope": "full_sample",
        **deepcopy(material_slots or {}),
    }
    return build_material_authority(
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        original_intent=original,
        material_slots=slots,
        runtime_material=(
            runtime_material
            if runtime_material is not None
            else _runtime_material_for_contract(
                _source_contract(source_run_id)
            )
        ),
        obligation_rejection_history=obligation_rejection_history,
    )


def _seed_waiting_run(
    *,
    run_id="run-source",
    thread_id="thread-source",
    topic_id="topic-source",
):
    store = InMemoryConversationStore()
    store.create_thread(thread_id, owner_id="user-1")
    contract = _source_contract(run_id)
    authority = _signed_material_authority(
        source_run_id=run_id,
        thread_id=thread_id,
        topic_id=topic_id,
    )
    store.upsert_run(
        run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        status="waiting_for_clarification",
        request={"material_authority": authority},
    )
    store.analysis_runtime_authority["analysis_contract"][
        contract["analysis_contract_id"]
    ] = contract
    return store, contract, authority


def test_material_authority_uses_current_goal_axes_and_execution_v3():
    authority = _signed_material_authority()

    validated = validate_material_authority(
        authority,
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        require_execution_material=True,
    )

    assert validated["schema_version"] == "4"
    assert validated["execution_material"]["schema_version"] == "3"
    assert validated["intent_material"]["goal_bindings"] == _GOAL_BINDINGS
    assert set(validated["route_material_slots"]) == {
        "target_metrics",
        "component_ids",
        "association_metric_ids",
        "dimension_ids",
        "baselines",
        "context_sources",
        "claim_types",
        "required_outcomes",
        "analysis_axis_ids",
        "diagnostic_tags",
        "scope",
    }


def test_material_authority_rejects_owner_and_signature_drift():
    authority = _signed_material_authority()

    with pytest.raises(EvidenceIntegrityError, match="material_authority_owner_mismatch"):
        validate_material_authority(
            authority,
            source_run_id="run-source",
            thread_id="thread-other",
            topic_id="topic-source",
        )

    forged = deepcopy(authority)
    forged["route_material_slots"]["baselines"] = ["previous_day"]
    with pytest.raises(
        EvidenceIntegrityError,
        match="material_authority_signature_invalid",
    ):
        validate_material_authority(
            forged,
            source_run_id="run-source",
            thread_id="thread-source",
            topic_id="topic-source",
        )



def test_completed_material_authority_finalizes_and_resolves_current_shape():
    store, contract, authority = _seed_waiting_run()
    store.runs["run-source"]["status"] = "running_workflow"

    finalized = store.finalize_completed_material_authority(
        run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
        request={"question": "昨天付费金额为什么变化？"},
        material_authority=authority,
    )
    resolved = store.resolve_completed_material_authority(
        source_run_id="run-source",
        thread_id="thread-source",
        topic_id="topic-source",
    )

    assert finalized["analysis_contract"] == canonical_value(contract)
    assert resolved["analysis_contract"]["analysis_contract_id"] == (
        contract["analysis_contract_id"]
    )
    assert resolved["material_authority"] == authority
    assert store.runs["run-source"]["status"] == "completed"


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


def test_postgres_completed_material_authority_finalization_is_owner_bound():
    contract = _source_contract()
    authority = _signed_material_authority(
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
        material_authority=authority,
    )

    assert finalized["analysis_contract"] == canonical_value(contract)
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_postgres_completed_material_authority_rolls_back_owner_drift():
    contract = _source_contract()
    authority = _signed_material_authority(
        thread_id="thread-1",
        topic_id="topic-1",
    )
    connection = _CompletedFinalizationConnection(
        run_row={
            "run_status": "running_workflow",
            "run_thread_id": "thread-other",
            "run_topic_id": "topic-1",
            "run_request": json.dumps({}),
            "analysis_run_id": "run-source",
            "stored_contract_signature": contract["contract_signature"],
            "contract_payload": json.dumps(contract),
        }
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="completed_followup_owner_mismatch",
    ):
        PostgresConversationStore(
            connection
        ).finalize_completed_material_authority(
            run_id="run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            request={},
            material_authority=authority,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
