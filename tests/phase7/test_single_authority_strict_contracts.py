from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from bi_agent.conversation.agent_core import _workflow_authority_request
from bi_agent.runtime import langgraph_workflow
from bi_agent.runtime.durable_call_journal import InMemoryDurableCallJournal
from bi_agent.runtime.langgraph_workflow import (
    WorkflowFailure,
    run_single_authority_workflow,
)
from bi_agent.runtime.llm_client import LLMOutputError
from bi_agent.runtime.plan_authority import (
    ClaimObligation,
    EvidenceRequirement,
    PlanAuthorityContractError,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


_RUNTIME_REGISTRY = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


class _NoCallProvider:
    def invoke_json(self, **_kwargs):
        raise AssertionError("invalid_request_must_fail_before_provider_call")


class _ReleaseResolver:
    def resolve_dataset_release(self, _release_ref):
        raise AssertionError("invalid_request_must_fail_before_release_resolution")


class _PoisonThenValidProvider:
    durable_max_attempts = 2

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke_json(self, **kwargs):
        self.calls.append(dict(kwargs))
        output = {"decision": "poison" if len(self.calls) == 1 else "accepted"}
        return SimpleNamespace(
            output=output,
            audit={
                "task": kwargs["task"],
                "provider": "strict-contract-test",
                "model": "strict-contract-test",
                "prompt_version": kwargs["prompt_version"],
                "structured_output": output,
                "attempt_count": 1,
            },
        )


def _workflow_request() -> dict[str, object]:
    return {
        "run_id": "run-request-contract",
        "run_attempt_id": "run-request-contract",
        "question": "分析付费金额变化",
        "authority_store": object(),
        "llm_client": _NoCallProvider(),
        "runtime_registry": _RUNTIME_REGISTRY,
        "release_resolver": _ReleaseResolver(),
        "stop_after_phase": "phase02",
    }


@pytest.mark.parametrize(
    ("mutate", "failure_code"),
    (
        (
            lambda request: request.pop("run_id"),
            "single_authority_run_identity_invalid",
        ),
        (
            lambda request: request.pop("run_attempt_id"),
            "single_authority_run_identity_invalid",
        ),
        (
            lambda request: request.update({"run_attempt_id": "run-other"}),
            "single_authority_run_identity_invalid",
        ),
        (
            lambda request: request.update({"unknown_runtime_field": True}),
            "single_authority_request_shape_invalid",
        ),
        (
            lambda request: request.update(
                {"runtime": {"llm_client": request.pop("llm_client")}}
            ),
            "single_authority_request_shape_invalid",
        ),
    ),
)
def test_workflow_rejects_noncurrent_request_shapes_before_execution(
    mutate,
    failure_code: str,
) -> None:
    request = _workflow_request()
    mutate(request)

    with pytest.raises(WorkflowFailure, match=f"^{failure_code}$"):
        run_single_authority_workflow(request)


def test_langgraph_always_binds_validator_to_durable_wrapper() -> None:
    provider = _PoisonThenValidProvider()
    store = SimpleNamespace(attempt_journal=InMemoryDurableCallJournal())
    state = {
        "request": {"authority_store": store},
        "run_id": "run-durable-validator",
        "intent_revision": {"intent_revision_id": "intent-durable-validator"},
        "llm_client": provider,
        "llm_calls": [],
    }

    def reject_poison(output) -> None:
        if output.get("decision") == "poison":
            raise LLMOutputError("planner_contract_rejected")

    output = langgraph_workflow._invoke_llm(
        state,
        "single_authority_plan_proposal",
        {"question": "same durable input"},
        output_validator=reject_poison,
    )

    assert output == {"decision": "accepted"}
    assert len(provider.calls) == 2
    assert all("output_validator" not in call for call in provider.calls)
    assert len(state["provider_attempt_refs"]["compile_authoritative_plan"]) == 1


@pytest.mark.parametrize(
    ("mutate", "failure_code"),
    (
        (
            lambda request: request.pop("runtime_registry"),
            "single_authority_runtime_dependency_invalid:runtime_registry",
        ),
        (
            lambda request: request.update({"stop_after_phase": "phase03"}),
            "single_authority_runtime_dependency_invalid:analysis_runtime",
        ),
        (
            lambda request: request.update(
                {"stop_after_phase": "phase04", "analysis_runtime": object()}
            ),
            "single_authority_runtime_dependency_invalid:authority_connection,delivery_transport,destination_ref,locale,owner_ref,publication_channel,thread_id",
        ),
        (
            lambda request: request.update({"stop_after_phase": "phase06"}),
            "single_authority_stop_after_phase_invalid",
        ),
        (
            lambda request: request.update({"recursion_limit": 0}),
            "single_authority_recursion_limit_invalid",
        ),
        (
            lambda request: request.update(
                {"supersedes_intent_revision_id": "intent:prior"}
            ),
            "single_authority_revision_context_invalid",
        ),
        (
            lambda request: request.update(
                {"context_manifest": {"manifest_id": "conversation-only"}}
            ),
            "single_authority_request_shape_invalid",
        ),
    ),
)
def test_workflow_validates_phase_dependencies_and_revision_context(
    mutate,
    failure_code: str,
) -> None:
    request = _workflow_request()
    mutate(request)

    with pytest.raises(WorkflowFailure, match=f"^{failure_code}$"):
        run_single_authority_workflow(request)


def test_agent_core_projects_only_authority_runtime_fields() -> None:
    request = {
        **_workflow_request(),
        "thread_id": "thread-authority-projection",
        "turn_id": "turn-conversation-only",
        "topic_id": "topic-conversation-only",
        "user_message": "conversation-only",
        "context_manifest": {"manifest_id": "conversation-only"},
        "conversation_entry": {"entry": "conversation-only"},
        "turn_intent": "new_topic",
        "topic_relation": "new_topic",
        "topic_selection": {"source_run_id": "conversation-only"},
        "artifact_root": "conversation-only",
        "analysis_context": {"conversation": "only"},
    }

    projected = _workflow_authority_request(request)

    assert set(projected) == {
        "run_id",
        "run_attempt_id",
        "question",
        "authority_store",
        "llm_client",
        "runtime_registry",
        "release_resolver",
        "stop_after_phase",
    }


def _user_required_subject() -> dict[str, object]:
    return {
        "target_metric_ref": "paid_amount",
        "scope": {"scope_type": "full_sample", "filters": []},
        "outcome_refs": ("outcome:explain_change",),
        "goal_refs": ("explain_change",),
    }


def _analyst_auxiliary_subject() -> dict[str, object]:
    return {
        "planner_proposal_ref": "planner-proposal:test",
        "proposal_item_ref": "proposal-item:test",
        "target_metric_refs": ("paid_amount",),
        "scope": {"scope_type": "full_sample", "filters": []},
        "goal_refs": ("explain_change",),
    }


def _obligation(role: str, subject: dict[str, object]) -> ClaimObligation:
    return ClaimObligation.create(
        claim_kind="comparative_change",
        role=role,
        subject=subject,
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=("observed",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "directional",
        },
    )


def test_claim_obligation_accepts_only_current_role_specific_subjects() -> None:
    user_required = _obligation("user_required", _user_required_subject())
    analyst_auxiliary = _obligation("analyst_auxiliary", _analyst_auxiliary_subject())

    assert set(user_required.subject) == {
        "target_metric_ref",
        "scope",
        "outcome_refs",
        "goal_refs",
    }
    assert set(analyst_auxiliary.subject) == {
        "planner_proposal_ref",
        "proposal_item_ref",
        "target_metric_refs",
        "scope",
        "goal_refs",
    }


def test_claim_obligation_from_dict_rejects_legacy_subject_shape() -> None:
    payload = _obligation("user_required", _user_required_subject()).to_dict()
    payload["subject"] = {"metric_ref": "paid_amount"}

    with pytest.raises(
        PlanAuthorityContractError,
        match="^claim_obligation_subject_invalid$",
    ):
        ClaimObligation.from_dict(payload)


def test_claim_obligation_rejects_legacy_evidence_field() -> None:
    payload = _obligation("user_required", _user_required_subject()).to_dict()
    payload["minimum_evidence"] = payload.pop("evidence_requirement")["evidence_kinds"]

    with pytest.raises(
        PlanAuthorityContractError,
        match="^claim_obligation_shape_invalid$",
    ):
        ClaimObligation.from_dict(payload)


def test_evidence_requirement_supports_only_explicit_any_of() -> None:
    with pytest.raises(
        PlanAuthorityContractError,
        match="^evidence_requirement_operator_invalid$",
    ):
        EvidenceRequirement.create(
            operator="all_of",
            evidence_kinds=("observed", "statistical_association"),
        )


@pytest.mark.parametrize(
    ("role", "base_subject", "mutate"),
    (
        (
            "user_required",
            _user_required_subject,
            lambda subject: subject.update({"metric_ref": "paid_amount"}),
        ),
        (
            "user_required",
            _user_required_subject,
            lambda subject: subject.update({"outcome_refs": "outcome:change"}),
        ),
        (
            "user_required",
            _user_required_subject,
            lambda subject: subject.update({"goal_refs": "explain_change"}),
        ),
        (
            "user_required",
            _user_required_subject,
            lambda subject: subject.pop("goal_refs"),
        ),
        (
            "user_required",
            _user_required_subject,
            lambda subject: subject.update({"outcome_refs": ()}),
        ),
        (
            "analyst_auxiliary",
            _analyst_auxiliary_subject,
            lambda subject: subject.update({"target_metric_refs": "paid_amount"}),
        ),
        (
            "analyst_auxiliary",
            _analyst_auxiliary_subject,
            lambda subject: subject.update({"goal_refs": "explain_change"}),
        ),
        (
            "analyst_auxiliary",
            _analyst_auxiliary_subject,
            lambda subject: subject.pop("planner_proposal_ref"),
        ),
        (
            "analyst_auxiliary",
            _analyst_auxiliary_subject,
            lambda subject: subject.update({"target_metric_refs": ()}),
        ),
    ),
)
def test_claim_obligation_rejects_alias_scalar_and_missing_provenance(
    role: str,
    base_subject,
    mutate,
) -> None:
    subject = deepcopy(base_subject())
    mutate(subject)

    with pytest.raises(
        PlanAuthorityContractError,
        match="^claim_obligation_subject_invalid$",
    ):
        _obligation(role, subject)
