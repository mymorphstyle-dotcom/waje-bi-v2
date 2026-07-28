from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
import json
from typing import Any, Mapping

import pytest

import bi_agent.runtime.langgraph_workflow as langgraph_workflow
import bi_agent.runtime.narrative_workflow as narrative_workflow_module
import bi_agent.runtime.post_execution_workflow as workflow
from bi_agent.runtime.authority_seal_persistence import AuthoritySealResult
from bi_agent.runtime.claim_settlement import ClaimSettlement
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.durable_call_journal import (
    DurableCallJournalError,
    InMemoryDurableCallJournal,
)
from bi_agent.runtime.llm_client import (
    LLMOutputError,
    LLMProviderError,
    LLMResult,
)
from bi_agent.runtime.narrative_authority import PublicationFieldVisibilityPolicy
from bi_agent.runtime.narrative_context import build_narrative_answer_context
from bi_agent.runtime.narrative_material_persistence import (
    NarrativeMaterialPersistenceResult,
)
from bi_agent.runtime.narrative_materialization import (
    build_public_limitation_contexts,
    build_reviewed_public_materialization,
)
from bi_agent.runtime.narrative_workflow import (
    NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT,
    NarrativeAnswerContext,
    prepare_narrative_material_projection,
    run_narrative_workflow,
)
from bi_agent.runtime.public_fact_materialization import materialize_public_facts
from bi_agent.runtime.publication_flow import PublicationFlowResult
from bi_agent.runtime.publication_persistence import (
    DeliveryMessage,
    DeliveryPersistenceResult,
    DeliveryTransportResult,
    PublicationPersistenceError,
    PublicationPersistenceOperationalError,
    PublicationPersistenceResult,
    narrative_publication_transition_payloads,
)
from bi_agent.runtime.post_seal_failure_persistence import (
    PostSealFailurePersistenceResult,
    PostSealFailureTerminal,
)
from bi_agent.runtime.single_authority import DurableTransition, LifecycleState
from tests.phase7.test_authority_seal_persistence import (
    _SemanticLLM,
    _fixture,
)


def _provider_material_facts(
    material: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    columns = material.get("fact_columns")
    if columns is None:
        return tuple(dict(item) for item in material["facts"])
    return tuple(
        dict(zip(columns, row, strict=True))
        for row in material["facts"]
    )


class _NarrativeLLM:
    supports_output_validator = True

    def __init__(self) -> None:
        self.calls = 0

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        payload = json.loads(kwargs["messages"][1]["content"])
        self.calls += 1
        if kwargs["task"].endswith("narrative_writer"):
            projection = payload["material_projection"]
            claim = projection["claims"][0]
            material_by_handle = {
                item["material_handle"]: item
                for item in projection["evidence_materials"]
            }
            fact = _provider_material_facts(
                material_by_handle[claim["material_handles"][0]]
            )[0]
            recommendations = projection["recommendations"]
            output = {
                "blocks": [
                    {
                        "role": "executive_answer",
                        "text": "Accepted executive answer statement.",
                        "c": [claim["claim_handle"]],
                        "r": (
                            [recommendations[0]["recommendation_handle"]]
                            if recommendations
                            else []
                        ),
                        "l": list(claim["limitation_handles"]),
                        "f": [[claim["claim_handle"], fact["fact_handle"]]],
                        "s": "business_finding",
                        "q": True,
                    }
                ]
            }
        else:
            output = {
                "decisions": [
                    {
                        "block_id": block["block_id"],
                        "disposition": "accepted",
                        "reason_code": None,
                        "affected_claim_handles": [],
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
                "provider": "provider:test",
                "model": "model:narrative-test",
                "attempt_count": 1,
                "response_id": f"response:narrative:{self.calls}",
                "raw_response_content": raw,
                "structured_output": output,
            },
        )


class _NoLLM:
    def __init__(self) -> None:
        self.calls = 0

    def invoke_json(self, **_: Any) -> Any:
        self.calls += 1
        raise AssertionError("accepted_transition_replay_called_llm")


class _NoSensitiveOutput:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_: Any) -> tuple[()]:
        self.calls += 1
        return ()


@pytest.fixture(autouse=True)
def _persist_pre_provider_material(monkeypatch: pytest.MonkeyPatch) -> None:
    def persist(*_args: Any, **kwargs: Any) -> NarrativeMaterialPersistenceResult:
        projection = kwargs["projection"]
        palette = kwargs["palette"]
        bundle = kwargs["authority_bundle"]
        return NarrativeMaterialPersistenceResult(
            projection_ref=projection.projection_ref,
            projection_digest=projection.content_digest,
            palette_ref=palette.palette_ref,
            run_attempt_id=bundle.run_attempt_id,
            status="replayed",
        )

    monkeypatch.setattr(workflow, "persist_narrative_material_projection", persist)


@dataclass(frozen=True)
class _AcceptedFixture:
    source: Any
    policy: PublicationFieldVisibilityPolicy
    narrative: Any
    flow: PublicationFlowResult
    compose_transition: DurableTransition
    compose_input: Mapping[str, Any]
    compose_output: Mapping[str, Any]


def _accepted_fixture() -> _AcceptedFixture:
    source = _fixture()
    semantic = source.semantic_result
    policy = PublicationFieldVisibilityPolicy.fixed(
        policy_id="aggregate-answer",
        revision=1,
        restricted_output_policy_ref="test-policy:raw-identifiers",
        restricted_output_policy_version="1",
        restricted_output_fields=("order_id", "user_id"),
    )
    evidence_entries = tuple(
        entry
        for _, _, entries, _ in source.execution.capability_outcome_bundles
        for entry in entries
    )
    public_facts = materialize_public_facts(
        authority_bundle=source.bundle,
        authority_namespace=semantic.settlement.authority_namespace,
        claims=semantic.settlement.accepted_claims,
        claim_keys=semantic.settlement.accepted_claim_keys,
        support_edges=semantic.settlement.accepted_support_edges,
        evidence_entries=evidence_entries,
        visibility_policy=policy,
    )
    contexts = build_public_limitation_contexts(
        source.execution,
        source.bundle,
        semantic.settlement,
        semantic.recommendations,
    )
    reviewed = build_reviewed_public_materialization(
        authority_bundle=source.bundle,
        claim_settlement=semantic.settlement,
        public_fact_materialization=public_facts,
        public_limitation_context_by_ref=contexts,
    )
    context = build_narrative_answer_context(
        authority_bundle=source.bundle,
        authority_inputs=semantic.authority_bundle_inputs,
        intent_revision=source.intent,
        recommendations=semantic.recommendations,
        locale="en-US",
    )
    _, material_projection = prepare_narrative_material_projection(
        authority_bundle=source.bundle,
        claim_settlement=semantic.settlement,
        evidence_entries=evidence_entries,
        recommendations=semantic.recommendations,
        public_materialization=reviewed,
        visibility_policy=policy,
    )
    narrative = run_narrative_workflow(
        authority_bundle=source.bundle,
        claim_settlement=semantic.settlement,
        evidence_entries=evidence_entries,
        recommendations=semantic.recommendations,
        public_materialization=reviewed,
        visibility_policy=policy,
        material_projection=material_projection,
        answer_context=context,
        llm_client=_NarrativeLLM(),
        sensitive_output_inspector=_NoSensitiveOutput(),
    )
    flow = PublicationFlowResult.create(
        authority_inputs=semantic.authority_bundle_inputs,
        authority_bundle=source.bundle,
        claim_settlement=semantic.settlement,
        recommendations=semantic.recommendations,
        narrative_workflow=narrative,
        supersedes_publication=None,
        destination_ref="conversation:post-execution-test",
        channel="conversation",
        published_at="2026-07-18T12:30:00Z",
    )
    compose_input, compose_output = narrative_publication_transition_payloads(
        authority_inputs=semantic.authority_bundle_inputs,
        authority_bundle=source.bundle,
        claim_settlement=semantic.settlement,
        recommendations=semantic.recommendations,
        narrative_workflow=narrative,
        publication_flow=flow,
        supersedes_publication=None,
    )
    compose_transition = DurableTransition.create(
        node_name="compose_claim_aware_narrative",
        parent_transition_id=source.settlement_transition.transition_id,
        run_attempt_id=source.bundle.run_attempt_id,
        intent_revision_id=source.bundle.intent_revision_id,
        decision_ledger_position=(
            source.settlement_transition.decision_ledger_position
        ),
        input_digest=canonical_digest(compose_input),
        output_digest=canonical_digest(compose_output),
        execution_attempt=1,
        provider_ref="waje-narrative-authority",
        model_ref="single-authority-phase05.v21",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="deliver_publication",
        started_at="2026-07-18T12:01:00Z",
        finished_at="2026-07-18T12:02:00Z",
    )
    return _AcceptedFixture(
        source=source,
        policy=policy,
        narrative=narrative,
        flow=flow,
        compose_transition=compose_transition,
        compose_input=compose_input,
        compose_output=compose_output,
    )


def test_narrative_context_carries_customer_metric_labels_to_the_writer() -> None:
    source = _fixture()
    context = build_narrative_answer_context(
        authority_bundle=source.bundle,
        authority_inputs=source.semantic_result.authority_bundle_inputs,
        intent_revision=source.intent,
        recommendations=source.semantic_result.recommendations,
        locale="zh-CN",
        customer_term_labels={
            "paid_amount": "付费金额",
            "paid_users": "付费用户数",
        },
    )

    assert (
        'customer_term_labels={"paid_amount":"付费金额","paid_users":"付费用户数"}'
        in context.business_context
    )
    writer_context = context.to_writer_payload()
    assert "user_question" not in writer_context
    assert writer_context["accepted_intent_context"]["comparison_spec"] == (
        source.intent.comparison_spec
    )
    assert writer_context["accepted_intent_context"]["requested_analysis_axes"] == list(
        source.intent.requested_analysis_axes
    )
    assert writer_context["accepted_intent_context"]["requested_factor_refs"] == list(
        source.intent.requested_factor_refs
    )
    assert writer_context["accepted_plan_context"]["user_required_obligations"]
    assert writer_context["accepted_plan_context"]["analysis_axes"]
    assert writer_context["accepted_plan_context"]["capability_route"]


def test_internal_post_execution_result_does_not_replay_semantic_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _fixture()

    def forbidden_replay(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "typed_internal_post_execution_must_not_replay_semantic_authority"
        )

    monkeypatch.setattr(
        type(source.semantic_result),
        "replay",
        forbidden_replay,
    )
    monkeypatch.setattr(
        type(source.bundle),
        "from_dict",
        classmethod(forbidden_replay),
    )

    result = workflow._build_result(
        status="authority_sealed",
        semantic=source.semantic_result,
        bundle=source.bundle,
        authority_transition=source.settlement_transition,
        claim_coverage_checkpoint_ref=(source.claim_coverage_checkpoint.checkpoint_ref),
        claim_coverage_checkpoint_digest=(
            source.claim_coverage_checkpoint.content_digest
        ),
        claim_coverage_transition_id=(source.claim_coverage_checkpoint.transition_id),
        authority_persistence_status="inserted",
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
    )

    assert result.status == "authority_sealed"


def test_internal_post_execution_result_hashes_typed_dependency_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _fixture()

    def forbidden_serialization(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "typed_post_execution_manifest_must_not_serialize_semantic_graph"
        )

    monkeypatch.setattr(
        type(source.semantic_result),
        "to_dict",
        forbidden_serialization,
    )

    result = workflow._build_result(
        status="authority_sealed",
        semantic=source.semantic_result,
        bundle=source.bundle,
        authority_transition=source.settlement_transition,
        claim_coverage_checkpoint_ref=(source.claim_coverage_checkpoint.checkpoint_ref),
        claim_coverage_checkpoint_digest=(
            source.claim_coverage_checkpoint.content_digest
        ),
        claim_coverage_transition_id=(source.claim_coverage_checkpoint.transition_id),
        authority_persistence_status="inserted",
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
    )

    assert result.semantic_authority_result is source.semantic_result


def test_langgraph_internal_post_execution_stage_does_not_rehydrate_typed_result() -> (
    None
):
    stage_source = inspect.getsource(langgraph_workflow._run_post_execution_stage)

    assert ".replay()" not in stage_source


def test_internal_build_result_does_not_replay_typed_narrative_or_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()

    def forbidden_rehydration(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("typed_narrative_flow_must_not_be_rehydrated")

    monkeypatch.setattr(
        type(fixture.narrative),
        "replay",
        forbidden_rehydration,
    )
    monkeypatch.setattr(
        PublicationFlowResult,
        "from_dict",
        classmethod(forbidden_rehydration),
    )

    result = workflow._build_result(
        status="narrative_ready",
        semantic=fixture.source.semantic_result,
        bundle=fixture.source.bundle,
        authority_transition=fixture.source.settlement_transition,
        claim_coverage_checkpoint_ref=(
            fixture.source.claim_coverage_checkpoint.checkpoint_ref
        ),
        claim_coverage_checkpoint_digest=(
            fixture.source.claim_coverage_checkpoint.content_digest
        ),
        claim_coverage_transition_id=(
            fixture.source.claim_coverage_checkpoint.transition_id
        ),
        authority_persistence_status="replayed",
        material_projection_ref=(fixture.narrative.material_projection.projection_ref),
        material_projection_digest=(
            fixture.narrative.material_projection.content_digest
        ),
        material_persistence_status="replayed",
        narrative=fixture.narrative,
        flow=fixture.flow,
        compose_transition=fixture.compose_transition,
        narrative_persistence_status="replayed",
        customer_payload_ref=workflow._customer_payload_ref(
            flow=fixture.flow,
            narrative=fixture.narrative,
        ),
        delivery_attempt_ref=None,
        delivery_status=None,
        delivery_replayed=None,
        customer_publication_ref=None,
        customer_payload=None,
    )

    assert result.narrative_workflow is fixture.narrative
    assert result.publication_flow is fixture.flow


def test_transition_payloads_do_not_replay_typed_narrative_or_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()

    def forbidden_rehydration(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("typed_transition_inputs_must_not_be_rehydrated")

    monkeypatch.setattr(
        type(fixture.narrative),
        "replay",
        forbidden_rehydration,
    )
    monkeypatch.setattr(
        PublicationFlowResult,
        "from_dict",
        classmethod(forbidden_rehydration),
    )

    transition_input, transition_output = narrative_publication_transition_payloads(
        authority_inputs=fixture.source.semantic_result.authority_bundle_inputs,
        authority_bundle=fixture.source.bundle,
        claim_settlement=fixture.source.semantic_result.settlement,
        recommendations=fixture.source.semantic_result.recommendations,
        narrative_workflow=fixture.narrative,
        publication_flow=fixture.flow,
        supersedes_publication=None,
    )

    assert transition_input["authority_bundle_ref"] == fixture.source.bundle.bundle_ref
    assert transition_output["publication_state"] == "ready"


def test_narrative_typed_settlement_validation_does_not_rehydrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()

    def forbidden_rehydration(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("typed_claim_settlement_must_not_be_rehydrated")

    monkeypatch.setattr(
        ClaimSettlement,
        "from_dict",
        classmethod(forbidden_rehydration),
    )

    settlement = narrative_workflow_module._validated_settlement(
        fixture.source.semantic_result.settlement
    )

    assert settlement is fixture.source.semantic_result.settlement


class _AcceptedStore:
    def __init__(self, fixture: _AcceptedFixture) -> None:
        self.fixture = fixture
        self.attempt_journal = InMemoryDurableCallJournal()
        self.attempt_journal.bind_stage(
            run_attempt_id=fixture.source.execution.run_attempt_id,
            transition_attempt_id=fixture.source.settlement_transition.attempt_id,
            stage_name="settle_claim_authority",
            attempt_refs=(),
        )
        self.attempt_journal.bind_stage(
            run_attempt_id=fixture.source.execution.run_attempt_id,
            transition_attempt_id=fixture.compose_transition.attempt_id,
            stage_name="compose_claim_aware_narrative",
            attempt_refs=(),
        )

    def load_accepted_transition(
        self,
        *,
        run_attempt_id: str,
        node_name: str,
        input_digest: str,
    ) -> Mapping[str, Any] | None:
        assert run_attempt_id == self.fixture.source.execution.run_attempt_id
        if node_name == "settle_claim_authority":
            transition = self.fixture.source.settlement_transition
            expected_input = self.fixture.source.settlement_transition_input
            expected_output = self.fixture.source.settlement_transition_output
        elif node_name == "compose_claim_aware_narrative":
            transition = self.fixture.compose_transition
            expected_input = self.fixture.compose_input
            expected_output = self.fixture.compose_output
        else:
            raise AssertionError(f"unexpected transition node: {node_name}")
        assert input_digest == canonical_digest(expected_input)
        return {
            "transition": transition,
            "input_payload": expected_input,
            "output_payload": expected_output,
        }

    def load_post_seal_failure_terminal(self, **_: Any) -> None:
        return None

    def record_post_seal_failure(self, **_: Any) -> Any:
        raise AssertionError("unexpected_post_seal_failure")


class _EmptyStore:
    def __init__(self) -> None:
        self.attempt_journal = InMemoryDurableCallJournal()

    def load_accepted_transition(self, **_: Any) -> None:
        return None

    def load_post_seal_failure_terminal(self, **_: Any) -> None:
        return None

    def record_post_seal_failure(self, **_: Any) -> Any:
        raise AssertionError("unexpected_post_seal_failure")


class _NoTransitionReloadStore(_EmptyStore):
    def load_accepted_transition(self, **_: Any) -> None:
        raise AssertionError("typed_continuation_reloaded_transition")


class _FailureStore(_AcceptedStore):
    def __init__(self, fixture: _AcceptedFixture) -> None:
        super().__init__(fixture)
        self.terminal: PostSealFailureTerminal | None = None
        self.history: list[PostSealFailureTerminal] = []
        self.record_calls = 0

    def load_accepted_transition(self, **kwargs: Any) -> Mapping[str, Any] | None:
        if kwargs["node_name"] == "compose_claim_aware_narrative":
            return None
        return super().load_accepted_transition(**kwargs)

    def load_post_seal_failure_terminal(self, **_: Any) -> Any:
        return self.terminal

    def record_post_seal_failure(self, **kwargs: Any) -> Any:
        self.record_calls += 1
        bundle = kwargs["authority_bundle"]
        prior_ref = kwargs["supersedes_terminal_ref"]
        if self.terminal is None:
            assert prior_ref is None
            lifecycle = LifecycleState.create(
                run_attempt_id=bundle.run_attempt_id,
                execution_state="complete",
                evidence_state=(
                    "boundary_only"
                    if bundle.authority_mode == "boundary_only"
                    else "complete"
                ),
                retry_state="exhausted",
            )
            attempt_number = 1
        else:
            assert prior_ref == self.terminal.terminal_ref
            assert self.terminal.failure_record.retryability == "retryable"
            lifecycle = self.terminal.lifecycle_state.transition(
                retry_state="exhausted"
            )
            attempt_number = self.terminal.attempt_number + 1
        self.terminal = PostSealFailureTerminal.create(
            attempt_number=attempt_number,
            supersedes_terminal_ref=prior_ref,
            status=kwargs["status"],
            authority_bundle=bundle,
            authority_transition=kwargs["authority_transition"],
            failure_record=kwargs["failure_record"],
            lifecycle_state=lifecycle,
        )
        self.history.append(self.terminal)
        return PostSealFailurePersistenceResult(
            terminal=self.terminal,
            status="inserted",
        )


class _RaisingLLM:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def invoke_json(self, **_: Any) -> Any:
        self.calls += 1
        raise self.error


def _run_failure_path(
    fixture: _AcceptedFixture,
    *,
    store: _FailureStore,
    client: Any,
) -> workflow.PostExecutionWorkflowResult:
    return workflow.run_post_execution_workflow(
        fixture.source.execution,
        claim_coverage_checkpoint=fixture.source.claim_coverage_checkpoint,
        intent_revision=fixture.source.intent,
        owner_ref="owner:authority-seal",
        thread_ref="thread:authority-seal",
        authority_store=store,
        connection=_Connection(),
        llm_client=client,
        visibility_policy=fixture.policy,
        sensitive_output_inspector=_NoSensitiveOutput(),
        locale="en-US",
        destination_ref="conversation:post-execution-test",
        channel="conversation",
        transport=None,
        stop_after="phase05",
    )


class _Connection:
    def execute(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("unexpected_connection_execute")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_accepted_transition_replay_uses_zero_llm_and_duplicate_dispatch_is_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()
    store = _AcceptedStore(fixture)
    client = _NoLLM()
    inspector = _NoSensitiveOutput()
    transport_calls: list[DeliveryMessage] = []
    dispatched = False

    def transport(message: DeliveryMessage) -> DeliveryTransportResult:
        transport_calls.append(message)
        return DeliveryTransportResult.published("receipt:post-execution")

    def deliver(
        _connection: Any,
        *,
        outbox_ref: str,
        transport: Any,
    ) -> DeliveryPersistenceResult:
        nonlocal dispatched
        assert outbox_ref == fixture.flow.outbox.outbox_ref
        if not dispatched:
            message = DeliveryMessage(
                outbox_ref=fixture.flow.outbox.outbox_ref,
                destination_ref=fixture.flow.outbox.destination_ref,
                channel=fixture.flow.outbox.channel,
                idempotency_key=fixture.flow.outbox.idempotency_key,
                customer_payload=fixture.flow.customer_payload,
            )
            result = transport(message)
            assert result.status == "published"
            dispatched = True
            replayed = False
        else:
            replayed = True
        return DeliveryPersistenceResult(
            outbox_ref=outbox_ref,
            attempt_ref="delivery-attempt:post-execution",
            status="published",
            lifecycle_state_digest="a" * 64,
            customer_publication_ref="customer-publication:post-execution",
            replayed=replayed,
        )

    monkeypatch.setattr(workflow, "deliver_persisted_outbox", deliver)
    monkeypatch.setattr(
        workflow,
        "_persisted_customer_payload",
        lambda *_args, **_kwargs: fixture.flow.customer_payload,
    )

    def run():
        return workflow.run_post_execution_workflow(
            fixture.source.execution,
            claim_coverage_checkpoint=(fixture.source.claim_coverage_checkpoint),
            intent_revision=fixture.source.intent,
            owner_ref="owner:authority-seal",
            thread_ref="thread:authority-seal",
            authority_store=store,
            connection=_Connection(),
            llm_client=client,
            visibility_policy=fixture.policy,
            sensitive_output_inspector=inspector,
            locale="en-US",
            destination_ref="conversation:post-execution-test",
            channel="conversation",
            transport=transport,
        )

    first = run()
    second = run()

    assert first.status == "completed"
    assert first.delivery_replayed is False
    assert second.status == "completed"
    assert second.delivery_replayed is True
    assert second.authority_persistence_status == "replayed"
    assert second.narrative_persistence_status == "replayed"
    assert second.customer_payload == fixture.flow.customer_payload
    assert second.replay() == second
    assert client.calls == 0
    assert inspector.calls == 0
    assert len(transport_calls) == 1


def test_phase05_continuation_reuses_prior_authority_without_transition_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()
    prior = workflow._build_result(
        status="authority_sealed",
        semantic=fixture.source.semantic_result,
        bundle=fixture.source.bundle,
        authority_transition=fixture.source.settlement_transition,
        claim_coverage_checkpoint_ref=(
            fixture.source.claim_coverage_checkpoint.checkpoint_ref
        ),
        claim_coverage_checkpoint_digest=(
            fixture.source.claim_coverage_checkpoint.content_digest
        ),
        claim_coverage_transition_id=(
            fixture.source.claim_coverage_checkpoint.transition_id
        ),
        authority_persistence_status="inserted",
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
    )

    def persist_success(*_args: Any, **kwargs: Any) -> PublicationPersistenceResult:
        narrative = kwargs["narrative_workflow"]
        flow = kwargs["publication_flow"]
        transition = kwargs["compose_transition"]
        return PublicationPersistenceResult(
            narrative_workflow_ref=(
                "narrative-workflow-result:sha256:" + narrative.content_digest
            ),
            narrative_workflow_digest=narrative.content_digest,
            transition_id=transition.transition_id,
            publication_ref=flow.publication.publication_ref,
            outbox_ref=flow.outbox.outbox_ref,
            customer_payload_ref=workflow._customer_payload_ref(
                flow=flow,
                narrative=narrative,
            ),
            publication_state="verified",
            status="inserted",
            lifecycle_state_digest="a" * 64,
        )

    monkeypatch.setattr(workflow, "persist_publication", persist_success)
    result = workflow.run_post_execution_workflow(
        fixture.source.execution,
        claim_coverage_checkpoint=fixture.source.claim_coverage_checkpoint,
        intent_revision=fixture.source.intent,
        owner_ref="owner:authority-seal",
        thread_ref="thread:authority-seal",
        authority_store=_NoTransitionReloadStore(),
        connection=_Connection(),
        llm_client=_NarrativeLLM(),
        visibility_policy=fixture.policy,
        sensitive_output_inspector=_NoSensitiveOutput(),
        locale="en-US",
        destination_ref="conversation:post-execution-test",
        channel="conversation",
        transport=None,
        stop_after="phase05",
        prior_result=prior,
    )

    assert result.status == "narrative_ready"
    assert result.semantic_authority_result is prior.semantic_authority_result
    assert result.authority_transition is prior.authority_transition


def test_delivery_continuation_skips_reload_and_public_rematerialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()
    customer_payload_ref = workflow._customer_payload_ref(
        flow=fixture.flow,
        narrative=fixture.narrative,
    )
    prior = workflow._build_result(
        status="narrative_ready",
        semantic=fixture.source.semantic_result,
        bundle=fixture.source.bundle,
        authority_transition=fixture.source.settlement_transition,
        claim_coverage_checkpoint_ref=(
            fixture.source.claim_coverage_checkpoint.checkpoint_ref
        ),
        claim_coverage_checkpoint_digest=(
            fixture.source.claim_coverage_checkpoint.content_digest
        ),
        claim_coverage_transition_id=(
            fixture.source.claim_coverage_checkpoint.transition_id
        ),
        authority_persistence_status="inserted",
        material_projection_ref=(fixture.narrative.material_projection.projection_ref),
        material_projection_digest=(
            fixture.narrative.material_projection.content_digest
        ),
        material_persistence_status="inserted",
        narrative=fixture.narrative,
        flow=fixture.flow,
        compose_transition=fixture.compose_transition,
        narrative_persistence_status="inserted",
        customer_payload_ref=customer_payload_ref,
        delivery_attempt_ref=None,
        delivery_status=None,
        delivery_replayed=None,
        customer_publication_ref=None,
        customer_payload=None,
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("typed_delivery_continuation_recomputed_evidence")

    monkeypatch.setattr(workflow, "_accepted_evidence_entries", forbidden)
    monkeypatch.setattr(workflow, "materialize_public_facts", forbidden)
    monkeypatch.setattr(
        workflow,
        "deliver_persisted_outbox",
        lambda *_args, **_kwargs: DeliveryPersistenceResult(
            outbox_ref=fixture.flow.outbox.outbox_ref,
            attempt_ref="delivery-attempt:typed-continuation",
            status="published",
            lifecycle_state_digest="b" * 64,
            customer_publication_ref="customer-publication:typed-continuation",
            replayed=False,
        ),
    )
    monkeypatch.setattr(
        workflow,
        "_persisted_customer_payload",
        lambda *_args, **_kwargs: fixture.flow.customer_payload,
    )
    no_llm = _NoLLM()
    result = workflow.run_post_execution_workflow(
        fixture.source.execution,
        claim_coverage_checkpoint=fixture.source.claim_coverage_checkpoint,
        intent_revision=fixture.source.intent,
        owner_ref="owner:authority-seal",
        thread_ref="thread:authority-seal",
        authority_store=_NoTransitionReloadStore(),
        connection=_Connection(),
        llm_client=no_llm,
        visibility_policy=fixture.policy,
        sensitive_output_inspector=_NoSensitiveOutput(),
        locale="en-US",
        destination_ref="conversation:post-execution-test",
        channel="conversation",
        transport=lambda _message: DeliveryTransportResult.published(
            "receipt:typed-continuation"
        ),
        prior_result=prior,
    )

    assert result.status == "completed"
    assert result.customer_payload == fixture.flow.customer_payload
    assert (
        workflow.validate_in_process_post_execution_workflow_result(result)
        is result
    )
    with pytest.raises(
        workflow.PostExecutionWorkflowError,
        match="^post_execution_result_integrity_invalid$",
    ):
        workflow.validate_in_process_post_execution_workflow_result(
            replace(result, publication_ref="publication:tampered")
        )
    assert no_llm.calls == 0


def test_invalid_prior_is_exposed_without_persistence_fallback() -> None:
    fixture = _accepted_fixture()
    prior = workflow._build_result(
        status="authority_sealed",
        semantic=fixture.source.semantic_result,
        bundle=fixture.source.bundle,
        authority_transition=fixture.source.settlement_transition,
        claim_coverage_checkpoint_ref=(
            fixture.source.claim_coverage_checkpoint.checkpoint_ref
        ),
        claim_coverage_checkpoint_digest=(
            fixture.source.claim_coverage_checkpoint.content_digest
        ),
        claim_coverage_transition_id=(
            fixture.source.claim_coverage_checkpoint.transition_id
        ),
        authority_persistence_status="inserted",
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
    )

    with pytest.raises(
        workflow.PostExecutionWorkflowError,
        match="^post_execution_prior_result_invalid$",
    ):
        workflow.run_post_execution_workflow(
            fixture.source.execution,
            claim_coverage_checkpoint=fixture.source.claim_coverage_checkpoint,
            intent_revision=fixture.source.intent,
            owner_ref="owner:authority-seal",
            thread_ref="thread:authority-seal",
            authority_store=_NoTransitionReloadStore(),
            connection=_Connection(),
            llm_client=_NoLLM(),
            visibility_policy=fixture.policy,
            sensitive_output_inspector=_NoSensitiveOutput(),
            locale="en-US",
            destination_ref="conversation:post-execution-test",
            channel="conversation",
            transport=None,
            stop_after="phase05",
            prior_result=replace(prior, content_digest="0" * 64),
        )


def test_phase04_stop_seals_authority_without_narrative_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    client = _SemanticLLM()
    inspector = _NoSensitiveOutput()
    seal_calls = 0

    def seal(_connection: Any, **kwargs: Any) -> AuthoritySealResult:
        nonlocal seal_calls
        seal_calls += 1
        assert kwargs["claim_coverage_checkpoint"] == (
            fixture.claim_coverage_checkpoint
        )
        assert kwargs["settlement_transition"].parent_transition_id == (
            fixture.claim_coverage_checkpoint.transition_id
        )
        bundle = kwargs["authority_bundle"]
        return AuthoritySealResult(
            bundle_ref=bundle.bundle_ref,
            bundle_digest=bundle.bundle_digest,
            status="inserted",
            lifecycle_state_digest="b" * 64,
        )

    monkeypatch.setattr(workflow, "seal_authority_bundle", seal)

    result = workflow.run_post_execution_workflow(
        fixture.execution,
        claim_coverage_checkpoint=fixture.claim_coverage_checkpoint,
        intent_revision=fixture.intent,
        owner_ref="owner:authority-seal",
        thread_ref="thread:authority-seal",
        authority_store=_EmptyStore(),
        connection=_Connection(),
        llm_client=client,
        visibility_policy=PublicationFieldVisibilityPolicy.fixed(
            policy_id="aggregate-answer",
            revision=1,
            restricted_output_policy_ref="test-policy:raw-identifiers",
            restricted_output_policy_version="1",
            restricted_output_fields=("order_id", "user_id"),
        ),
        sensitive_output_inspector=inspector,
        locale="en-US",
        destination_ref="",
        channel="",
        transport=None,
        stop_after="phase04",
    )

    assert result.status == "authority_sealed"
    assert result.narrative_workflow is None
    assert result.publication_ref is None
    assert result.customer_payload is None
    assert result.claim_coverage_checkpoint_ref == (
        fixture.claim_coverage_checkpoint.checkpoint_ref
    )
    assert result.claim_coverage_checkpoint_digest == (
        fixture.claim_coverage_checkpoint.content_digest
    )
    assert result.claim_coverage_transition_id == (
        fixture.claim_coverage_checkpoint.transition_id
    )
    assert result.replay() == result
    assert client.calls == 3
    assert inspector.calls == 0
    assert seal_calls == 1


def test_post_execution_has_no_default_claim_coverage_checkpoint() -> None:
    fixture = _fixture()
    client = _SemanticLLM()

    with pytest.raises(
        workflow.PostExecutionWorkflowError,
        match="^post_execution_claim_coverage_checkpoint_invalid$",
    ):
        workflow.run_post_execution_workflow(
            fixture.execution,
            claim_coverage_checkpoint=None,  # type: ignore[arg-type]
            intent_revision=fixture.intent,
            owner_ref="owner:authority-seal",
            thread_ref="thread:authority-seal",
            authority_store=_EmptyStore(),
            connection=_Connection(),
            llm_client=client,
            visibility_policy=PublicationFieldVisibilityPolicy.fixed(
                policy_id="aggregate-answer",
                revision=1,
                restricted_output_policy_ref="test-policy:raw-identifiers",
                restricted_output_policy_version="1",
                restricted_output_fields=("order_id", "user_id"),
            ),
            sensitive_output_inspector=_NoSensitiveOutput(),
            locale="en-US",
            destination_ref="conversation:post-execution-test",
            channel="conversation",
            transport=None,
            stop_after="phase04",
        )

    assert client.calls == 0


class _RowCursor:
    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = row

    def fetchone(self) -> Mapping[str, Any]:
        return self.row


class _PersistedPayloadConnection:
    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = row
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement: str, params: Mapping[str, Any]) -> _RowCursor:
        assert "post_execution_persisted_customer_payload" in statement
        assert params["outbox_ref"] == self.row["customer_payload_record"]["outbox_ref"]
        return _RowCursor(self.row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_completed_payload_is_read_back_from_persisted_customer_publication() -> None:
    fixture = _accepted_fixture()
    payload = fixture.flow.customer_payload
    customer_body = {
        "run_attempt_id": fixture.flow.outbox.run_attempt_id,
        "outbox_ref": fixture.flow.outbox.outbox_ref,
        "publication_ref": fixture.flow.publication.publication_ref,
        "publication_digest": fixture.flow.publication.publication_digest,
        "projection_id": fixture.flow.projection.projection_id,
        "projection_digest": fixture.flow.projection.projection_digest,
        "field_visibility_policy_ref": fixture.policy.policy_ref,
        "field_visibility_policy_digest": fixture.policy.content_digest,
        "customer_payload_digest": canonical_digest(payload),
        "customer_payload": payload,
    }
    customer_digest = canonical_digest(customer_body)
    customer_ref = "customer-payload:sha256:" + customer_digest
    publication_body = {
        "run_attempt_id": fixture.flow.outbox.run_attempt_id,
        "outbox_ref": fixture.flow.outbox.outbox_ref,
        "delivery_attempt_ref": "delivery-attempt:post-execution",
        "publication_ref": fixture.flow.publication.publication_ref,
        "projection_id": fixture.flow.projection.projection_id,
        "destination_ref": fixture.flow.outbox.destination_ref,
        "channel": fixture.flow.outbox.channel,
        "transport_receipt_ref": "receipt:post-execution",
    }
    publication_digest = canonical_digest(publication_body)
    publication_ref = "customer-publication:sha256:" + publication_digest
    connection = _PersistedPayloadConnection(
        {
            "customer_payload_ref": customer_ref,
            "customer_payload": payload,
            "customer_payload_record": {
                "customer_payload_ref": customer_ref,
                **customer_body,
                "content_digest": customer_digest,
            },
            "customer_publication_ref": publication_ref,
            "customer_publication_record": {
                "customer_publication_ref": publication_ref,
                **publication_body,
                "content_digest": publication_digest,
            },
        }
    )
    delivery = DeliveryPersistenceResult(
        outbox_ref=fixture.flow.outbox.outbox_ref,
        attempt_ref="delivery-attempt:post-execution",
        status="published",
        lifecycle_state_digest="c" * 64,
        customer_publication_ref=publication_ref,
        replayed=False,
    )

    replayed = workflow._persisted_customer_payload(
        connection,
        owner_ref="owner:authority-seal",
        bundle=fixture.source.bundle,
        narrative=fixture.narrative,
        flow=fixture.flow,
        customer_payload_ref=customer_ref,
        delivery=delivery,
    )

    assert replayed == payload
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_narrative_input_budget_failure_is_persisted_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()
    store = _FailureStore(fixture)
    client = _NarrativeLLM()
    oversized_context = NarrativeAnswerContext.create(
        user_question="Why did paid amount change?",
        answer_goal="Explain the verified movement.",
        locale="en-US",
        business_context=("界" * (NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT + 1),),
        accepted_intent_context=(
            fixture.narrative.answer_context.accepted_intent_context
        ),
        accepted_plan_context=(
            fixture.narrative.answer_context.accepted_plan_context
        ),
    )
    monkeypatch.setattr(
        workflow,
        "build_narrative_answer_context",
        lambda **_kwargs: oversized_context,
    )

    result = _run_failure_path(fixture, store=store, client=client)

    assert result.status == "narrative_failed"
    assert result.narrative_workflow is None
    assert result.post_seal_failure_terminal is not None
    failure = result.post_seal_failure_terminal.failure_record
    assert failure.kind == "narrative_input_budget_exceeded"
    assert failure.retryability == "not_retryable"
    assert client.calls == 0
    assert store.record_calls == 1


def test_retryable_provider_failure_attempt_chain_recovers_without_resealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()
    store = _FailureStore(fixture)
    client = _RaisingLLM(
        LLMProviderError(
            kind="provider_rate_limited",
            retryability="retryable",
        )
    )

    first = _run_failure_path(fixture, store=store, client=client)

    assert first.status == "narrative_failed"
    assert first.authority_bundle == fixture.source.bundle
    assert first.authority_transition == fixture.source.settlement_transition
    assert first.narrative_workflow is None
    assert first.publication_ref is None
    assert first.delivery_status is None
    assert first.post_seal_failure_persistence_status == "inserted"
    assert first.post_seal_failure_terminal is not None
    assert first.post_seal_failure_terminal.failure_record.layer == "narrative"
    assert first.post_seal_failure_terminal.failure_record.scope == "run"
    assert first.post_seal_failure_terminal.failure_record.integrity_level == "local"
    assert (
        first.post_seal_failure_terminal.failure_record.kind == "provider_rate_limited"
    )
    assert first.replay() == first
    assert client.calls == 1
    assert store.record_calls == 1
    assert first.authority_persistence_status == "replayed"

    monkeypatch.setattr(
        workflow,
        "seal_authority_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("authority_bundle_resealed")
        ),
    )
    second = _run_failure_path(fixture, store=store, client=client)
    assert second.status == "narrative_failed"
    assert second.authority_bundle == first.authority_bundle
    assert second.authority_persistence_status == "replayed"
    assert second.post_seal_failure_terminal is not None
    assert second.post_seal_failure_terminal.attempt_number == 2
    assert second.post_seal_failure_terminal.supersedes_terminal_ref == (
        first.post_seal_failure_terminal.terminal_ref
    )
    assert [item.attempt_number for item in store.history] == [1, 2]

    def persist_success(*_args: Any, **kwargs: Any) -> PublicationPersistenceResult:
        narrative = kwargs["narrative_workflow"]
        flow = kwargs["publication_flow"]
        transition = kwargs["compose_transition"]
        return PublicationPersistenceResult(
            narrative_workflow_ref=(
                "narrative-workflow-result:sha256:" + narrative.content_digest
            ),
            narrative_workflow_digest=narrative.content_digest,
            transition_id=transition.transition_id,
            publication_ref=flow.publication.publication_ref,
            outbox_ref=flow.outbox.outbox_ref,
            customer_payload_ref=workflow._customer_payload_ref(
                flow=flow,
                narrative=narrative,
            ),
            publication_state="verified",
            status="inserted",
            lifecycle_state_digest="f" * 64,
        )

    monkeypatch.setattr(workflow, "persist_publication", persist_success)
    recovered = _run_failure_path(
        fixture,
        store=store,
        client=_NarrativeLLM(),
    )
    assert recovered.status == "narrative_ready"
    assert recovered.authority_bundle == first.authority_bundle
    assert recovered.authority_persistence_status == "replayed"
    assert recovered.post_seal_failure_terminal is None
    assert len(store.history) == 2
    assert store.record_calls == 2


def test_non_retryable_provider_failure_replays_without_llm() -> None:
    fixture = _accepted_fixture()
    store = _FailureStore(fixture)
    first = _run_failure_path(
        fixture,
        store=store,
        client=_RaisingLLM(LLMOutputError("invalid provider output")),
    )

    no_llm = _NoLLM()
    replayed = _run_failure_path(fixture, store=store, client=no_llm)

    assert first.status == replayed.status == "narrative_failed"
    assert replayed.post_seal_failure_terminal == first.post_seal_failure_terminal
    assert replayed.post_seal_failure_persistence_status == "replayed"
    assert no_llm.calls == 0
    assert store.record_calls == 1


def test_provider_failure_text_does_not_change_structured_failure_scope() -> None:
    fixture = _accepted_fixture()
    results = []
    for message in ("first provider output text", "unrelated second text"):
        results.append(
            _run_failure_path(
                fixture,
                store=_FailureStore(fixture),
                client=_RaisingLLM(LLMOutputError(message)),
            )
        )

    left = results[0].post_seal_failure_terminal
    right = results[1].post_seal_failure_terminal
    assert left is not None and right is not None
    assert left.failure_record == right.failure_record
    assert left.failure_record.policy_scope == (
        "narrative",
        "provider_output_invalid",
        "run",
        left.failure_record.affected_refs,
        "local",
        "not_retryable",
        False,
    )


@pytest.mark.parametrize(
    "error",
    (
        DurableCallJournalError("journal_integrity_invalid"),
        AssertionError("programming_error"),
    ),
)
def test_non_provider_errors_after_seal_remain_integrity_failures(
    error: BaseException,
) -> None:
    fixture = _accepted_fixture()
    store = _FailureStore(fixture)

    with pytest.raises(type(error), match=str(error)):
        _run_failure_path(
            fixture,
            store=store,
            client=_RaisingLLM(error),
        )

    assert store.terminal is None
    assert store.record_calls == 0


def test_publication_operational_failure_is_typed_but_closure_failure_bubbles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _accepted_fixture()
    detail_ref = "technical-detail:sha256:" + "9" * 64

    monkeypatch.setattr(
        workflow,
        "persist_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PublicationPersistenceOperationalError(technical_detail_ref=detail_ref)
        ),
    )
    store = _FailureStore(fixture)
    result = _run_failure_path(fixture, store=store, client=_NarrativeLLM())
    assert result.status == "publication_failed"
    assert result.authority_bundle == fixture.source.bundle
    assert result.post_seal_failure_terminal is not None
    failure = result.post_seal_failure_terminal.failure_record
    assert failure.layer == "persistence"
    assert failure.kind == "publication_persistence_unavailable"
    assert failure.integrity_level == "local"
    assert failure.retryability == "retryable"

    monkeypatch.setattr(
        workflow,
        "persist_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PublicationPersistenceError("publication_authority_closure_conflict")
        ),
    )
    closure_store = _FailureStore(fixture)
    with pytest.raises(
        PublicationPersistenceError,
        match="publication_authority_closure_conflict",
    ):
        _run_failure_path(
            fixture,
            store=closure_store,
            client=_NarrativeLLM(),
        )
    assert closure_store.terminal is None


def test_external_limitation_text_input_is_not_part_of_post_execution_contract() -> (
    None
):
    signature = inspect.signature(workflow.run_post_execution_workflow)

    assert "public_limitation_text_by_ref" not in signature.parameters
    assert "public_limitation_context_by_ref" not in signature.parameters
