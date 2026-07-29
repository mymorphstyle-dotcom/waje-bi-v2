from __future__ import annotations

from dataclasses import replace
import json
from typing import Any, Mapping

import pytest

from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    GatewayDeliveryTransport,
    _finalize_workflow_terminal,
)
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_digest
from bi_agent.runtime.factor_coverage import (
    FactorCoveragePlan,
    FactorCoveragePlanItem,
    build_investigation_branches,
    settle_factor_coverage,
    synthesize_factor_coverage,
)
from bi_agent.runtime.llm_client import LLMResult
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult
from bi_agent.runtime.narrative_workflow import (
    run_narrative_workflow,
    run_narrative_quality_audit,
)
from bi_agent.runtime.post_execution_workflow import PostExecutionWorkflowResult
import bi_agent.runtime.post_execution_workflow as post_execution
from bi_agent.runtime.post_seal_failure_persistence import PostSealFailureTerminal
from bi_agent.runtime.publication_persistence import DeliveryMessage
from bi_agent.runtime.single_authority import FailureRecord
from tests.phase7.test_narrative_workflow import _provider_material_facts
from tests.phase7.test_post_execution_workflow import _accepted_fixture


class _NoSensitiveOutput:
    def __call__(self, **_: Any) -> tuple[()]:
        return ()


class _BindingConnection:
    def execute(self, *_: Any, **__: Any) -> None:
        return None


class _BindingLLM:
    def invoke_json(self, **_: Any) -> None:
        return None


class _WithheldNarrativeLLM:
    supports_output_validator = True

    def __init__(self) -> None:
        self.calls = 0

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        payload = json.loads(kwargs["messages"][1]["content"])
        self.calls += 1
        if kwargs["task"].endswith("narrative_writer"):
            output = self._writer_output(payload)
        else:
            output = {
                "decisions": [
                    {
                        "block_id": block["block_id"],
                        "disposition": "vetoed",
                        "reason_code": "meaning_exceeds_publication_ceiling",
                        "affected_claim_handles": block["claim_handles"][:1],
                        "affected_recommendation_handles": [],
                        "limitation_handles": [],
                    }
                    for block in payload["blocks"]
                ]
            }
        validator = kwargs.get("output_validator")
        if validator is not None:
            validator(output)
        raw = json.dumps(output, sort_keys=True, separators=(",", ":"))
        return LLMResult(
            output=output,
            audit={
                "provider": "provider:agent-core-withheld-test",
                "model": "model:agent-core-withheld-test",
                "attempt_count": 1,
                "response_id": f"response:agent-core-withheld:{self.calls}",
                "raw_response_content": raw,
                "structured_output": output,
            },
        )

    @staticmethod
    def _writer_output(payload: Mapping[str, Any]) -> dict[str, Any]:
        material_projection = payload["material_projection"]
        claim = material_projection["claims"][0]
        materials_by_handle = {
            item["material_handle"]: item
            for item in material_projection["evidence_materials"]
        }
        fact = _provider_material_facts(
            materials_by_handle[claim["material_handles"][0]]
        )[0]
        recommendations = material_projection["recommendations"]
        return {
            "blocks": [
                _WithheldNarrativeLLM._compact_writer_block({
                    "role": "executive_answer",
                    "text": "Candidate executive answer wording.",
                    "claim_handles": [claim["claim_handle"]],
                    "recommendation_handles": (
                        [recommendations[0]["recommendation_handle"]]
                        if recommendations
                        else []
                    ),
                    "limitation_handles": list(claim["limitation_handles"]),
                    "material_fact_bindings": [
                        {
                            "claim_handle": claim["claim_handle"],
                            "fact_handle": fact["fact_handle"],
                        }
                    ],
                    "statement_role": "business_finding",
                    "required": True,
                })
            ]
        }

    @staticmethod
    def _compact_writer_block(
        block: Mapping[str, Any],
    ) -> dict[str, Any]:
        compact = {
            "role": block["role"],
            "text": block["text"],
            "p": list(block.get("requirement_handles", ())),
            "c": list(block["claim_handles"]),
            "r": list(block["recommendation_handles"]),
            "l": list(block["limitation_handles"]),
            "f": [
                [item["claim_handle"], item["fact_handle"]]
                for item in block["material_fact_bindings"]
            ],
            "s": block["statement_role"],
            "q": block["required"],
        }
        return compact


@pytest.fixture(scope="module")
def accepted_fixture():
    return _accepted_fixture()


def _coverage_result_args(fixture: Any) -> dict[str, str]:
    checkpoint = fixture.source.claim_coverage_checkpoint
    return {
        "claim_coverage_checkpoint_ref": checkpoint.checkpoint_ref,
        "claim_coverage_checkpoint_digest": checkpoint.content_digest,
        "claim_coverage_transition_id": checkpoint.transition_id,
    }


def _post_result(fixture: Any, status: str) -> PostExecutionWorkflowResult:
    if status == "authority_sealed":
        return post_execution._build_result(
            status=status,
            semantic=fixture.source.semantic_result,
            bundle=fixture.source.bundle,
            authority_transition=fixture.source.settlement_transition,
            authority_persistence_status="replayed",
            material_projection_ref=None,
            material_projection_digest=None,
            material_persistence_status="not_started",
            narrative=None,
            flow=None,
            compose_transition=None,
            narrative_persistence_status="not_started",
            customer_payload_ref=None,
            delivery_attempt_ref=None,
            delivery_status=None,
            delivery_replayed=None,
            customer_publication_ref=None,
            customer_payload=None,
            **_coverage_result_args(fixture),
        )
    customer_payload_ref = post_execution._customer_payload_ref(
        flow=fixture.flow,
        narrative=fixture.narrative,
    )
    if status == "narrative_ready":
        delivery_status = None
        delivery_attempt_ref = None
        delivery_replayed = None
        customer_publication_ref = None
        customer_payload = None
    elif status == "completed":
        delivery_status = "published"
        delivery_attempt_ref = "delivery-attempt:agent-core-test"
        delivery_replayed = False
        customer_publication_ref = "customer-publication:agent-core-test"
        customer_payload = fixture.flow.customer_payload
    else:
        delivery_status = {
            "delivery_retryable_failed": "retryable_failed",
            "delivery_permanently_failed": "permanently_failed",
        }[status]
        delivery_attempt_ref = "delivery-attempt:agent-core-test"
        delivery_replayed = False
        customer_publication_ref = None
        customer_payload = None
    return post_execution._build_result(
        status=status,
        semantic=fixture.source.semantic_result,
        bundle=fixture.source.bundle,
        authority_transition=fixture.source.settlement_transition,
        authority_persistence_status="replayed",
        material_projection_ref=fixture.narrative.material_projection.projection_ref,
        material_projection_digest=(
            fixture.narrative.material_projection.content_digest
        ),
        material_persistence_status="replayed",
        narrative=fixture.narrative,
        flow=fixture.flow,
        compose_transition=fixture.compose_transition,
        narrative_persistence_status="replayed",
        customer_payload_ref=customer_payload_ref,
        delivery_attempt_ref=delivery_attempt_ref,
        delivery_status=delivery_status,
        delivery_replayed=delivery_replayed,
        customer_publication_ref=customer_publication_ref,
        customer_payload=customer_payload,
        **_coverage_result_args(fixture),
    )


def _failure_post_result(
    fixture: Any,
    *,
    status: str,
    retryability: str,
) -> PostExecutionWorkflowResult:
    layer = "narrative" if status == "narrative_failed" else "persistence"
    failure = FailureRecord.create(
        layer=layer,
        kind=(
            "provider_rate_limited"
            if status == "narrative_failed"
            else "publication_persistence_unavailable"
        ),
        scope="run",
        affected_refs=(
            fixture.source.bundle.bundle_ref,
            fixture.source.settlement_transition.transition_id,
            fixture.narrative.material_projection.projection_ref,
        ),
        integrity_level="local",
        retryability=retryability,
        user_actionable=False,
        business_boundary=(
            "The accepted analysis remains authoritative; customer publication "
            "is unavailable for this run attempt."
        ),
        technical_detail_ref="technical-detail:sha256:" + "9" * 64,
    )
    lifecycle = fixture.source.lifecycle.transition(
        evidence_state=(
            "boundary_only"
            if fixture.source.bundle.authority_mode == "boundary_only"
            else "complete"
        )
    ).transition(retry_state="exhausted")
    terminal = PostSealFailureTerminal.create(
        attempt_number=1,
        supersedes_terminal_ref=None,
        status=status,
        authority_bundle=fixture.source.bundle,
        authority_transition=fixture.source.settlement_transition,
        failure_record=failure,
        lifecycle_state=lifecycle,
    )
    return post_execution._build_result(
        status=status,
        semantic=fixture.source.semantic_result,
        bundle=fixture.source.bundle,
        authority_transition=fixture.source.settlement_transition,
        authority_persistence_status="replayed",
        material_projection_ref=fixture.narrative.material_projection.projection_ref,
        material_projection_digest=(
            fixture.narrative.material_projection.content_digest
        ),
        material_persistence_status="replayed",
        narrative=None,
        flow=None,
        compose_transition=None,
        narrative_persistence_status="not_started",
        customer_payload_ref=None,
        delivery_attempt_ref=None,
        delivery_status=None,
        delivery_replayed=None,
        customer_publication_ref=None,
        customer_payload=None,
        failure_terminal=terminal,
        failure_persistence_status="inserted",
        **_coverage_result_args(fixture),
    )


def _coverage_refs(post_result: PostExecutionWorkflowResult) -> dict[str, Any]:
    execution = (
        post_result.semantic_authority_result.authority_bundle_inputs.execution_result
    )
    return {
        "schema_version": "claim-coverage-checkpoint.v1",
        "source_plan_revision_id": execution.plan_revision_id,
        "source_execution_result_ref": (execution.authoritative_execution_result_ref),
        "claim_coverage_checkpoint_ref": (post_result.claim_coverage_checkpoint_ref),
        "claim_coverage_checkpoint_digest": (
            post_result.claim_coverage_checkpoint_digest
        ),
        "claim_coverage_evaluation_ref": "claim-coverage-evaluation:test",
        "plan_expansion_decision_ref": "plan-expansion-decision:test",
        "decision": "seal",
        "plan_patch_ref": None,
        "accepted_transition_id": post_result.claim_coverage_transition_id,
    }


def _factor_coverage_bundle(source: Any) -> dict[str, Any]:
    execution = source.execution
    plan_revision = execution.plan_revision
    target_metric_refs = tuple(
        dict.fromkeys(
            metric_ref
            for axis in plan_revision.analysis_axes
            for metric_ref in axis.target_metric_refs
        )
    )
    assert len(target_metric_refs) == 1
    item = FactorCoveragePlanItem.create(
        factor_domain_id="payment_order_metric_chain",
        business_name="支付金额与订单指标链",
        role="required",
        axis_refs=tuple(axis.axis_id for axis in plan_revision.analysis_axes),
        capability_refs=tuple(
            dict.fromkeys(
                task.capability_id for task in plan_revision.capability_tasks
            )
        ),
        dataset_refs=tuple(
            item["dataset_id"] for item in source.context.dataset_coverage
        ),
        dimension_refs=(),
        reconciliation_group="payment_amount_bridge",
        task_refs=tuple(task.task_id for task in plan_revision.capability_tasks),
        source_refs=("contract:test-factor-coverage",),
    )
    plan = FactorCoveragePlan.create(
        run_attempt_id=execution.run_attempt_id,
        intent_revision_id=execution.intent_revision_id,
        plan_revision_id=execution.plan_revision_id,
        authority_context_ref=execution.authority_context_ref,
        runtime_contract_version="test-runtime.v1",
        runtime_contract_digest=canonical_digest({"contract": "test-runtime.v1"}),
        target_metric_ref=target_metric_refs[0],
        coverage_items=(item,),
    )
    result = settle_factor_coverage(plan=plan, execution_result=execution)
    assert set(result.outcomes[0].evidence_refs) == {
        entry.entry_ref
        for bundle in execution.capability_outcome_bundles
        for entry in bundle[2]
    }
    branches = build_investigation_branches(
        plan=plan,
        authority_context=source.context,
    )
    synthesis = synthesize_factor_coverage(
        plan=plan,
        coverage_result=result,
        claim_settlement=source.semantic_result.settlement,
    )
    return {
        "factor_coverage_plan": plan.to_dict(),
        "factor_coverage_result": result.to_dict(),
        "investigation_branches": tuple(item.to_dict() for item in branches),
        "investigation_synthesis": synthesis.to_dict(),
    }


def _finalize(
    post_result: PostExecutionWorkflowResult,
    *,
    source: Any,
    stop_after_phase: str | None = None,
) -> tuple[dict[str, Any], InMemoryConversationStore]:
    store = InMemoryConversationStore()
    store.load_authority_context = lambda run_id: (  # type: ignore[attr-defined]
        source.context if run_id == source.context.run_attempt_id else None
    )
    store.create_thread("thread-agent-core-cutover", owner_id="owner-agent-core")
    store.upsert_run(
        post_result.run_attempt_id,
        thread_id="thread-agent-core-cutover",
        turn_id="turn-agent-core-cutover",
        topic_id="topic-agent-core-cutover",
        status="running_workflow",
        request={
            "question": "paid amount",
            "claim_coverage_refs": _coverage_refs(post_result),
        },
    )
    result = WorkflowRunResult(
        status=post_result.status,
        run_id=post_result.run_attempt_id,
        post_execution_result=post_result,
        **_factor_coverage_bundle(source),
    )
    response = _finalize_workflow_terminal(
        store=store,
        result=result,
        run_id=post_result.run_attempt_id,
        thread_id="thread-agent-core-cutover",
        turn_id="turn-agent-core-cutover",
        topic_id="topic-agent-core-cutover",
        request={
            "question": "paid amount",
            **(
                {"stop_after_phase": stop_after_phase}
                if stop_after_phase is not None
                else {}
            ),
        },
        context_manifest={"manifest_id": "manifest-agent-core-cutover"},
        intent="analysis_request",
        topic_relation="new_topic",
        llm_calls=(),
    )
    return response, store


def test_agent_core_does_not_rehydrate_internal_typed_post_execution_result(
    accepted_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_rehydration(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("internal_typed_post_execution_must_not_be_rehydrated")

    monkeypatch.setattr(
        PostExecutionWorkflowResult,
        "from_dict",
        classmethod(forbidden_rehydration),
    )

    response, _store = _finalize(
        _post_result(accepted_fixture, "authority_sealed"),
        source=accepted_fixture.source,
        stop_after_phase="phase04",
    )

    assert response["status"] == "authority_sealed"


def test_agent_core_validates_typed_post_execution_manifest_without_deep_serialization(
    accepted_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_result = _post_result(accepted_fixture, "authority_sealed")

    def forbidden_serialization(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "typed_post_execution_validation_must_not_serialize_semantic_graph"
        )

    monkeypatch.setattr(
        type(post_result.semantic_authority_result),
        "to_dict",
        forbidden_serialization,
    )

    response, _store = _finalize(
        post_result,
        source=accepted_fixture.source,
        stop_after_phase="phase04",
    )

    assert response["status"] == "authority_sealed"


@pytest.mark.parametrize(
    ("post_status", "stop_after_phase", "run_status"),
    (
        ("authority_sealed", "phase04", "authority_sealed"),
        ("narrative_ready", "phase05", "narrative_ready"),
    ),
)
def test_phase45_stops_preserve_only_safe_authority_refs(
    accepted_fixture: Any,
    post_status: str,
    stop_after_phase: str,
    run_status: str,
) -> None:
    response, store = _finalize(
        _post_result(accepted_fixture, post_status),
        source=accepted_fixture.source,
        stop_after_phase=stop_after_phase,
    )

    assert response["status"] == run_status
    assert response["post_execution_status"] == post_status
    assert "customer_publication" not in response
    assert (
        store.get_run_state(accepted_fixture.source.execution.run_attempt_id)["status"]
        == run_status
    )


def test_published_completion_returns_only_persisted_customer_publication(
    accepted_fixture: Any,
) -> None:
    post_result = _post_result(accepted_fixture, "completed")
    response, store = _finalize(post_result, source=accepted_fixture.source)

    assert response["status"] == "completed"
    assert response["publication_status"] == "published"
    assert response["delivery_status"] == "published"
    assert response["customer_publication"] == {
        "customer_publication_ref": post_result.customer_publication_ref,
        "customer_payload_ref": post_result.customer_payload_ref,
        "publication_ref": post_result.publication_ref,
        "outbox_ref": post_result.outbox_ref,
        "payload": post_result.customer_payload,
    }
    assert "llm_calls" not in response
    serialized_response = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert "factor_coverage" not in serialized_response
    assert "investigation_branches" not in serialized_response
    assert "coverage_item_ref" not in serialized_response
    persisted = store.get_run_state(post_result.run_attempt_id)
    assert persisted["status"] == "completed"
    assert persisted["request"]["publication_refs"] == response["publication_refs"]
    factor_event = next(
        event
        for event in store.audit_events
        if event["event_type"] == "factor_coverage_settled"
    )
    assert factor_event["payload"]["schema_version"] == "factor-coverage-audit.v1"
    assert factor_event["payload"]["coverage_plan"]["coverage_items"]
    assert factor_event["payload"]["investigation_branches"]
    assert factor_event["payload"]["investigation_synthesis"]
    assert factor_event["payload"]["investigation_synthesis"][
        "ranked_factor_domain_refs"
    ] == ["payment_order_metric_chain"]


@pytest.mark.parametrize(
    ("post_status", "delivery_status"),
    (
        ("delivery_retryable_failed", "retryable_failed"),
        ("delivery_permanently_failed", "permanently_failed"),
    ),
)
def test_delivery_failure_is_orthogonal_to_analysis_completion(
    accepted_fixture: Any,
    post_status: str,
    delivery_status: str,
) -> None:
    response, store = _finalize(
        _post_result(accepted_fixture, post_status),
        source=accepted_fixture.source,
    )

    assert response["status"] == "completed"
    assert response["analysis_status"] == "complete"
    assert response["publication_status"] == "ready"
    assert response["delivery_status"] == delivery_status
    assert response["post_execution_status"] == post_status
    assert "customer_publication" not in response
    assert (
        store.get_run_state(accepted_fixture.source.execution.run_attempt_id)["status"]
        == "completed"
    )


@pytest.mark.parametrize(
    ("post_status", "retryability", "publication_status"),
    (
        ("narrative_failed", "not_retryable", "not_ready"),
        ("publication_failed", "retryable", "failed"),
    ),
)
def test_post_seal_operational_failure_keeps_analysis_authority_and_safe_projection(
    accepted_fixture: Any,
    post_status: str,
    retryability: str,
    publication_status: str,
) -> None:
    post_result = _failure_post_result(
        accepted_fixture,
        status=post_status,
        retryability=retryability,
    )
    response, store = _finalize(post_result, source=accepted_fixture.source)

    failure = post_result.post_seal_failure_terminal.failure_record
    assert response["status"] == "completed"
    assert response["analysis_status"] == "complete"
    assert response["post_execution_status"] == post_status
    assert response["publication_status"] == publication_status
    assert response["delivery_status"] == "pending"
    assert response["operational_failure"] == {
        "failure_ref": failure.failure_id,
        "layer": failure.layer,
        "kind": failure.kind,
        "retryability": retryability,
        "business_boundary": failure.business_boundary,
    }
    assert "technical_detail_ref" not in response["operational_failure"]
    assert response["publication_refs"]["authority_bundle_ref"] == (
        accepted_fixture.source.bundle.bundle_ref
    )
    assert response["publication_refs"]["post_seal_failure_terminal_ref"] == (
        post_result.post_seal_failure_terminal_ref
    )
    persisted = store.get_run_state(post_result.run_attempt_id)
    assert persisted["status"] == "completed"
    assert (
        persisted["request"]["operational_failure"] == response["operational_failure"]
    )


def test_verifier_findings_remain_advisory_for_agent_completion(
    accepted_fixture: Any,
) -> None:
    evidence_entries = accepted_fixture.source.semantic_result.authority_bundle_inputs.material_projection_evidence_entries()
    client = _WithheldNarrativeLLM()
    narrative = run_narrative_workflow(
        authority_bundle=accepted_fixture.source.bundle,
        claim_settlement=accepted_fixture.source.semantic_result.settlement,
        evidence_entries=evidence_entries,
        recommendations=accepted_fixture.source.semantic_result.recommendations,
        public_materialization=accepted_fixture.narrative.public_materialization,
        visibility_policy=accepted_fixture.policy,
        material_projection=accepted_fixture.narrative.material_projection,
        answer_context=accepted_fixture.narrative.answer_context,
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert narrative.publication_ready is True
    assert narrative.delivery_narrative is narrative.narratives[0]
    assert client.calls == 1
    quality_audit = run_narrative_quality_audit(
        source_customer_publication_ref=(
            "customer-publication:sha256:" + "b" * 64
        ),
        authority_bundle=accepted_fixture.source.bundle,
        claim_settlement=accepted_fixture.source.semantic_result.settlement,
        evidence_entries=evidence_entries,
        recommendations=accepted_fixture.source.semantic_result.recommendations,
        narrative_workflow=narrative,
        llm_client=client,
    )
    assert quality_audit.verifier_report.rejected_block_ids
    assert narrative.delivery_narrative is narrative.narratives[0]
    assert client.calls == 2


def test_tampered_post_execution_result_cannot_complete_run(
    accepted_fixture: Any,
) -> None:
    tampered = replace(
        _post_result(accepted_fixture, "completed"),
        customer_payload={"blocks": []},
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^post_execution_workflow_result_invalid$",
    ):
        _finalize(tampered, source=accepted_fixture.source)


def test_phase46_runtime_bindings_are_explicit_and_postgres_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAJE_CONTROLLED_INVESTIGATION_ENABLED", "1")
    connection = _BindingConnection()
    transport = GatewayDeliveryTransport()
    core = ConversationAgentCore(
        PostgresConversationStore(connection),
        conversation_llm_client=_BindingLLM(),
        post_execution_locale="zh-CN",
        publication_channel="gateway",
        delivery_transport=transport,
    )

    bindings = core._post_execution_runtime_bindings(
        owner_ref="owner-phase46",
        thread_id="thread-phase46",
    )

    assert bindings == {
        "owner_ref": "owner-phase46",
        "thread_id": "thread-phase46",
        "authority_connection": connection,
        "locale": "zh-CN",
        "destination_ref": "conversation:thread-phase46",
        "publication_channel": "gateway",
        "delivery_transport": transport,
        "controlled_investigation_enabled": True,
    }
    with pytest.raises(
        EvidenceIntegrityError,
        match="^post_execution_postgres_store_required$",
    ):
        ConversationAgentCore(
            InMemoryConversationStore(),
            conversation_llm_client=_BindingLLM(),
            post_execution_locale="zh-CN",
            publication_channel="gateway",
            delivery_transport=transport,
        )._post_execution_runtime_bindings(
            owner_ref="owner-phase46",
            thread_id="thread-phase46",
        )


def test_controlled_investigation_requires_explicit_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAJE_CONTROLLED_INVESTIGATION_ENABLED", raising=False)
    connection = _BindingConnection()
    core = ConversationAgentCore(
        PostgresConversationStore(connection),
        conversation_llm_client=_BindingLLM(),
        post_execution_locale="zh-CN",
        publication_channel="gateway",
        delivery_transport=GatewayDeliveryTransport(),
    )

    assert (
        core._post_execution_runtime_bindings(
            owner_ref="owner-phase46",
            thread_id="thread-phase46",
        )["controlled_investigation_enabled"]
        is False
    )


def test_phase46_runtime_binding_omissions_fail_closed() -> None:
    core = ConversationAgentCore(
        PostgresConversationStore(_BindingConnection()),
        conversation_llm_client=_BindingLLM(),
        publication_channel="gateway",
        delivery_transport=GatewayDeliveryTransport(),
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^post_execution_runtime_binding_missing$",
    ):
        core._post_execution_runtime_bindings(
            owner_ref="owner-phase46",
            thread_id="thread-phase46",
        )


def test_gateway_delivery_transport_is_the_explicit_product_channel() -> None:
    transport = GatewayDeliveryTransport()
    message = DeliveryMessage(
        outbox_ref="outbox:phase46",
        destination_ref="conversation:thread-phase46",
        channel="gateway",
        idempotency_key="delivery:phase46",
        customer_payload={"blocks": []},
    )

    first = transport(message)
    second = transport(message)

    assert first == second
    assert first.status == "published"
    assert first.transport_receipt_ref.startswith("gateway-publication:sha256:")
    with pytest.raises(
        EvidenceIntegrityError,
        match="^gateway_delivery_message_invalid$",
    ):
        transport(replace(message, channel="email"))
