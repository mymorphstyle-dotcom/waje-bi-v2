from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.runtime.baseline_semantics import CANONICAL_BASELINE_IDS
from bi_agent.runtime.analysis_contract_compiler import (
    compile_analysis_contract,
)
from bi_agent.runtime.analysis_runtime import (
    analysis_outcome_requires_preexecution_clarification,
)
from bi_agent.runtime.dataset_catalog import (
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
    DatasetCatalog,
    DatasetSnapshot,
)
from bi_agent.runtime.llm_prompts import build_prompt
from bi_agent.runtime.llm_client import LLMOutputError
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "snapshot:paid_order_success:candidate-claims",
        "paid_order_success",
        "paid_order_success_clean",
        "2026-07-04",
        "schema:paid_order_success:candidate-claims",
        (
            "business_date_lagos",
            "business_date",
            "paid_amount_ngn",
            "user_id",
            "order_id",
            "is_first_payment",
        ),
        "contracts/sources/paid-order-detail.source.yaml@0.2",
        ("analyst",),
        "2026-07-05T00:00:00+00:00",
        "active",
    )


class _ReleaseResolver:
    def __init__(self, record) -> None:
        self.record = record

    def resolve_dataset_release(self, release_ref):
        if release_ref != self.record.release_ref:
            raise KeyError(release_ref)
        return self.record


def _catalog() -> DatasetCatalog:
    snapshot = _snapshot()
    logical_id = "paid-order-success-candidate-claims"
    revision = "paid-order-success-load:sha256:candidate-claims"
    release_ref = dataset_snapshot_release_ref(
        logical_id,
        revision,
        (snapshot.snapshot_ref,),
    )
    released = replace(
        snapshot,
        logical_snapshot_id=logical_id,
        load_revision=revision,
        snapshot_id=logical_id,
        release_ref=release_ref,
        rows_content_hash="a" * 64,
    )
    record = build_dataset_release_authority_record(
        ({**released.to_dict(), "requires_release": True},)
    )
    authorized = replace(
        released,
        authority_record_ref=record.authority_record_ref,
    )
    return DatasetCatalog(
        (authorized,),
        release_resolver=_ReleaseResolver(record),
    )


def _intent() -> dict:
    return {
        "question_family": "paid_amount_change_explanation",
        "question_families": ["paid_amount_change_explanation"],
        "target_metric": "paid_amount",
        "target_semantic": "2026-06-01",
        "time_window": "2026-06-01",
        "scope": "full_sample",
        "baseline_candidates": ["previous_day"],
        "baseline_binding": {
            "confirmed": True,
            "source": "user_choice",
            "candidates": ["previous_day"],
        },
        "claim_intents": [
            "comparative_change",
            "formula_component_contribution",
        ],
        "requested_components": [
            "paid_users",
            "paid_orders",
            "first_paid_users",
            "paid_frequency",
            "avg_order_amount",
        ],
        "requested_dimensions": [],
        "context_sources": [],
    }


def _route() -> dict:
    return {
        "requested_nodes": [
            "compare_periods",
            "driver_decomposition",
            "answer_verify",
        ],
        "analysis_requirements": {
            "target_metrics": ["paid_amount"],
            "requested_components": [
                "paid_users",
                "paid_orders",
                "first_paid_users",
                "paid_frequency",
                "avg_order_amount",
            ],
            "requested_dimensions": [],
            "baselines": ["previous_day"],
            "context_sources": [],
            "dataset_requirements": ["paid_order_success"],
            "diagnostic_tags": [],
            "claim_intents": [
                "comparative_change",
                "formula_component_contribution",
                "baseline_stability",
            ],
            "scope": "full_sample",
        },
    }


def _compile(
    *,
    accepted_capabilities: tuple[str, ...],
    claim_intents: tuple[str, ...],
    required_claim_intents: tuple[str, ...],
    candidate_claim_intents: tuple[str, ...],
    auxiliary_baselines: tuple[str, ...] = (),
):
    return compile_analysis_contract(
        run_id="run-candidate-claim-routing",
        proposal={
            "question_families": ["paid_amount_change_explanation"],
            "target_metrics": ["paid_amount"],
            "requested_components": [],
            "requested_dimensions": [],
            "dataset_requirements": ["paid_order_success"],
            "target_semantic": "2026-06-01",
            "baselines": ["previous_day"],
            "auxiliary_baselines": list(auxiliary_baselines),
            "claim_intents": list(claim_intents),
            "required_claim_intents": list(required_claim_intents),
            "candidate_claim_intents": list(candidate_claim_intents),
            "scope": "full_sample",
        },
        accepted_capabilities=accepted_capabilities,
        catalog=_catalog(),
        registry=RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
        as_of=datetime.fromisoformat("2026-07-15T10:00:00+08:00"),
        permission_scope="analyst",
    )


def test_llm_candidate_claim_auto_routes_safe_auxiliary_query_without_replacing_primary_baseline():
    from bi_agent.runtime import langgraph_workflow as workflow

    route = _route()
    requested, reconciled = workflow.reconcile_analysis_route(
        tuple(route["requested_nodes"]),
        route,
        _intent(),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert "rolling_window_compare" in requested
    assert reconciled["analysis_requirements"]["baselines"] == [
        "previous_day"
    ]
    resolution = reconciled["claim_intent_resolution"]
    assert resolution["required_claim_intents"] == [
        "comparative_change",
        "formula_component_contribution",
    ]
    assert resolution["auxiliary_claim_intents"] == [
        "baseline_stability",
        "segment_contribution_or_mix_shift",
    ]
    assert resolution["primary_baselines"] == ["previous_day"]
    assert resolution["auxiliary_baselines"] == [
        "rolling_7_day_baseline"
    ]
    assert resolution["auto_routed_claim_intents"] == {
        "baseline_stability": {
            "capability_id": "rolling_window_compare",
            "evidence_status": "queryable",
            "publication_status": "evidence_required",
            "auxiliary_baselines": ["rolling_7_day_baseline"],
        },
        "segment_contribution_or_mix_shift": {
            "capability_id": "candidate_dimension_screen",
            "evidence_status": "queryable",
            "publication_status": "evidence_required",
            "auxiliary_baselines": [],
        },
    }


def test_required_only_route_emits_primary_baseline_resolution():
    from bi_agent.runtime import langgraph_workflow as workflow

    intent = _intent()
    intent["required_claim_intents"] = list(intent["claim_intents"])
    intent["candidate_claim_intents"] = []
    route = _route()
    route["analysis_requirements"]["claim_intents"] = list(
        intent["required_claim_intents"]
    )

    _, reconciled = workflow.reconcile_analysis_route(
        tuple(route["requested_nodes"]),
        route,
        intent,
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert reconciled["claim_intent_resolution"] == {
        "schema_version": "claim_intent_resolution.v1",
        "required_claim_intents": [
            "comparative_change",
            "formula_component_contribution",
        ],
        "auxiliary_claim_intents": ["segment_contribution_or_mix_shift"],
        "auto_routed_claim_intents": {
            "segment_contribution_or_mix_shift": {
                "capability_id": "candidate_dimension_screen",
                "evidence_status": "queryable",
                "publication_status": "evidence_required",
                "auxiliary_baselines": [],
            }
        },
        "degraded_claim_intents": {},
        "primary_baselines": ["previous_day"],
        "auxiliary_baselines": [],
    }


@pytest.mark.parametrize("baseline_id", CANONICAL_BASELINE_IDS)
def test_one_day_comparison_keeps_window_identity_out_of_group_role(
    baseline_id: str,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    intent = _intent()
    intent.update(
        {
            "pattern_family": "custom_baseline",
            "required_claim_intents": list(intent["claim_intents"]),
            "candidate_claim_intents": [],
            "baseline_candidates": [baseline_id],
            "baseline_binding": {
                "confirmed": True,
                "source": "user_choice",
                "candidates": [baseline_id],
            },
        }
    )

    bound = workflow._bind_one_day_comparison_pattern(intent)

    assert bound["baseline_binding"]["candidates"] == [baseline_id]
    assert bound["pattern_params"]["target_group"] == "target"
    assert bound["pattern_params"]["baseline_group"] == "baseline"


def _authoritative_driver_rows(baseline_id: str) -> tuple[dict, ...]:
    target = {
        "window_id": "target_day",
        "window_role": "target",
        "observation_key": "target-observation",
        "paid_amount": 120.0,
        "paid_users": 12.0,
        "paid_orders": 24.0,
        "first_paid_users": 3.0,
        "paid_frequency": 2.0,
        "avg_order_amount": 5.0,
    }
    baseline_days = 7 if baseline_id == "rolling_7_day_baseline" else 1
    baseline = tuple(
        {
            "window_id": baseline_id,
            "window_role": "baseline",
            "observation_key": f"baseline-observation-{index}",
            "paid_amount": 100.0,
            "paid_users": 10.0,
            "paid_orders": 20.0,
            "first_paid_users": 2.0,
            "paid_frequency": 2.0,
            "avg_order_amount": 5.0,
        }
        for index in range(baseline_days)
    )
    return (target, *baseline)


@pytest.mark.parametrize("baseline_id", CANONICAL_BASELINE_IDS)
def test_required_only_route_projects_any_primary_baseline_into_driver(
    baseline_id: str,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    intent = _intent()
    intent.update(
        {
            "pattern_family": "custom_baseline",
            "required_claim_intents": list(intent["claim_intents"]),
            "candidate_claim_intents": [],
            "baseline_candidates": [baseline_id],
            "baseline_binding": {
                "confirmed": True,
                "source": "user_choice",
                "candidates": [baseline_id],
            },
        }
    )
    intent = workflow._bind_one_day_comparison_pattern(intent)
    route = _route()
    route["analysis_requirements"]["baselines"] = [baseline_id]
    route["analysis_requirements"]["claim_intents"] = list(
        intent["required_claim_intents"]
    )
    _, reconciled = workflow.reconcile_analysis_route(
        tuple(route["requested_nodes"]),
        route,
        intent,
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )
    state = {
        "request": {
            "run_mode": "fixture",
            "runtime_rows_by_intent": {
                "component_driver_scan": _authoritative_driver_rows(
                    baseline_id
                ),
            },
        },
        "intent": intent,
        "analysis_route": reconciled,
    }

    projected_rows, params = workflow._comparison_rows_and_params(
        state,
        "driver_decomposition",
        params=workflow._driver_params(state),
        dimension_keys=(),
        period_key="period",
    )
    target_window_id = workflow._comparison_group_window_id(
        projected_rows,
        group_key=params["group_key"],
        group_value=params["target_group"],
    )
    baseline_window_id = workflow._comparison_group_window_id(
        projected_rows,
        group_key=params["group_key"],
        group_value=params["baseline_group"],
    )
    evidence = driver_decomposition(
        projected_rows,
        target_window_id=target_window_id,
        baseline_window_id=baseline_window_id,
        **params,
    )

    assert {row["group"] for row in projected_rows} == {
        "target",
        "baseline",
    }
    assert baseline_window_id == baseline_id
    assert evidence.typed_payload["target_window_id"] == "target_day"
    assert evidence.typed_payload["baseline_window_id"] == baseline_id
    assert evidence.typed_payload["decompositions"]


def test_runtime_request_keeps_auxiliary_baseline_and_claim_provenance_separate():
    from bi_agent.runtime import langgraph_workflow as workflow

    route = _route()
    requested, reconciled = workflow.reconcile_analysis_route(
        tuple(route["requested_nodes"]),
        route,
        _intent(),
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )
    reconciled["requested_nodes"] = list(requested)
    request = workflow._analysis_runtime_request(
        {
            "run_id": "run-candidate-runtime-request",
            "request": {
                "run_id": "run-candidate-runtime-request",
                "topic_id": "topic-candidate-runtime-request",
                "analysis_context": {
                    "as_of": "2026-07-15T10:00:00+08:00",
                },
                "run_mode": "fixture",
            },
            "intent": _intent(),
            "analysis_route": reconciled,
        }
    )

    assert request.proposal["baselines"] == ("previous_day",)
    assert request.proposal["auxiliary_baselines"] == [
        "rolling_7_day_baseline"
    ]
    assert request.proposal["required_claim_intents"] == [
        "comparative_change",
        "formula_component_contribution",
    ]
    assert request.proposal["candidate_claim_intents"] == [
        "baseline_stability",
        "segment_contribution_or_mix_shift",
    ]


def test_queryable_auxiliary_claim_adds_its_window_without_changing_primary_baseline():
    outcome = _compile(
        accepted_capabilities=(
            "compare_periods",
            "rolling_window_compare",
        ),
        claim_intents=("comparative_change", "baseline_stability"),
        required_claim_intents=("comparative_change",),
        candidate_claim_intents=("baseline_stability",),
        auxiliary_baselines=("rolling_7_day_baseline",),
    )

    assert outcome.analysis_contract.claim_intents == (
        "comparative_change",
        "baseline_stability",
    )
    assert [
        window.window_id for window in outcome.analysis_contract.resolved_windows
    ] == ["target_day", "previous_day", "rolling_7_day_baseline"]
    rolling_query = next(
        contract
        for contract in outcome.query_contracts
        if contract.query_intent == "daily_metric_baselines"
        and "rolling_7_day_baseline" in contract.window_refs
    )
    assert rolling_query.window_refs == (
        "target_day",
        "previous_day",
        "rolling_7_day_baseline",
    )


def test_unavailable_auxiliary_claim_degrades_only_its_branch_and_main_query_continues():
    outcome = _compile(
        accepted_capabilities=("compare_periods",),
        claim_intents=("comparative_change", "baseline_stability"),
        required_claim_intents=("comparative_change",),
        candidate_claim_intents=("baseline_stability",),
    )

    gap = next(
        item
        for item in outcome.analysis_contract.contract_gaps
        if item.affected_claim_types == ("baseline_stability",)
    )
    assert outcome.analysis_contract.claim_intents == (
        "comparative_change",
    )
    assert gap.gap_id == "claim_candidate:baseline_stability:unsupported"
    assert gap.affected_capabilities == ("analysis_contract",)
    assert gap.requires_clarification is False
    assert gap.diagnostic_context["claim_origin"] == "llm_auxiliary"
    assert outcome.query_contracts
    assert analysis_outcome_requires_preexecution_clarification(outcome) is False


def test_unsupported_user_required_claim_limits_only_its_conclusion():
    outcome = _compile(
        accepted_capabilities=("compare_periods",),
        claim_intents=("comparative_change", "baseline_stability"),
        required_claim_intents=(
            "comparative_change",
            "baseline_stability",
        ),
        candidate_claim_intents=(),
    )

    gap = next(
        item
        for item in outcome.analysis_contract.contract_gaps
        if item.affected_claim_types == ("baseline_stability",)
    )
    assert gap.requires_clarification is False
    assert gap.affected_capabilities == ("analysis_contract",)
    assert gap.diagnostic_context["claim_origin"] == "user_required"
    assert gap.diagnostic_context["publication_status"] == "unavailable"
    assert outcome.analysis_contract.claim_intents == ("comparative_change",)
    assert outcome.query_contracts
    assert analysis_outcome_requires_preexecution_clarification(outcome) is False


def test_intent_claim_roles_keep_model_expansion_auxiliary_through_compilation():
    from bi_agent.runtime import langgraph_workflow as workflow

    intent = _intent()
    intent["claim_intents"] = [
        "comparative_change",
        "formula_component_contribution",
        "observed_activity",
    ]
    intent["required_claim_intents"] = [
        "comparative_change",
        "formula_component_contribution",
    ]
    intent["candidate_claim_intents"] = ["observed_activity"]
    route = _route()
    route["analysis_requirements"]["claim_intents"] = list(
        intent["claim_intents"]
    )

    requested, reconciled = workflow.reconcile_analysis_route(
        tuple(route["requested_nodes"]),
        route,
        intent,
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    resolution = reconciled["claim_intent_resolution"]
    assert resolution["required_claim_intents"] == [
        "comparative_change",
        "formula_component_contribution",
    ]
    assert resolution["auxiliary_claim_intents"] == [
        "observed_activity",
        "segment_contribution_or_mix_shift",
    ]
    assert resolution["degraded_claim_intents"] == {
        "observed_activity": {
            "reason": "safe_supporting_capability_unavailable",
            "publication_status": "omitted",
        }
    }

    reconciled["requested_nodes"] = list(requested)
    runtime_request = workflow._analysis_runtime_request(
        {
            "run_id": "run-intent-claim-roles",
            "request": {
                "run_id": "run-intent-claim-roles",
                "topic_id": "topic-intent-claim-roles",
                "analysis_context": {
                    "as_of": "2026-07-15T10:00:00+08:00",
                },
                "run_mode": "fixture",
            },
            "intent": intent,
            "analysis_route": reconciled,
        }
    )
    assert runtime_request.proposal["required_claim_intents"] == [
        "comparative_change",
        "formula_component_contribution",
    ]
    assert runtime_request.proposal["candidate_claim_intents"] == [
        "observed_activity",
        "segment_contribution_or_mix_shift",
    ]


def test_business_intent_claim_roles_split_user_requirements_from_model_candidates():
    from bi_agent.runtime import langgraph_workflow as workflow

    normalized = workflow._validated_business_intent_requirements(
        {
            "context_sources": [],
            "claim_intents": [
                "comparative_change",
                "formula_component_contribution",
                "observed_activity",
            ],
            "claim_intent_roles": {
                "comparative_change": "user_required",
                "formula_component_contribution": "user_required",
                "observed_activity": "llm_candidate",
            },
            "requested_dimensions": [],
            "requested_components": [],
        },
        RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
    )

    assert normalized["required_claim_intents"] == [
        "comparative_change",
        "formula_component_contribution",
    ]
    assert normalized["candidate_claim_intents"] == ["observed_activity"]


def test_business_intent_prompt_requires_claim_roles_and_explains_activity_semantics():
    spec = build_prompt(
        "business_intent",
        {
            "allowed_claim_types": [
                "comparative_change",
                "observed_activity",
            ],
            "allowed_claim_semantics": {
                "comparative_change": "目标与基线之间的变化",
                "observed_activity": "业务活动或玩法上下文的观测",
            },
        },
    )
    prompt = "\n".join(message["content"] for message in spec.messages)

    assert "claim_intent_roles" in prompt
    assert "user_required" in prompt
    assert "llm_candidate" in prompt
    assert "observed_activity" in prompt
    assert "业务活动" in prompt


def test_business_intent_prompt_keeps_previous_day_primary_and_stability_auxiliary():
    spec = build_prompt(
        "business_intent",
        {
            "allowed_claim_types": [
                "comparative_change",
                "formula_component_contribution",
                "baseline_stability",
            ],
        },
    )
    prompt = "\n".join(message["content"] for message in spec.messages)

    assert "previous_day" in prompt
    assert "primary baseline" in prompt
    assert "baseline_stability" in prompt
    assert "llm_candidate" in prompt
    assert "must not replace" in prompt


def test_local_policy_rejects_required_claim_incompatible_with_business_family():
    from bi_agent.runtime import langgraph_workflow as workflow

    output = {
        "question_family": "paid_amount_change_explanation",
        "target_metric": "paid_amount",
        "pattern_family": "custom_baseline",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "2026-06-01",
        "target_claim": "解释付费金额变化及其影响因素",
        "baseline_candidates": [],
        "analysis_requirements": {
            "context_sources": [],
            "claim_intents": [
                "comparative_change",
                "formula_component_contribution",
                "observed_activity",
            ],
            "claim_intent_roles": {
                "comparative_change": "user_required",
                "formula_component_contribution": "user_required",
                "observed_activity": "user_required",
            },
            "requested_dimensions": [],
            "requested_components": [],
        },
        "status_message": "已识别付费金额变化问题。",
        "display_summary": "准备验证变化方向并分析影响因素。",
    }
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )

    with pytest.raises(
        LLMOutputError,
        match="business_intent_contract_invalid:claim_intent_roles:observed_activity",
    ):
        workflow._validate_business_intent_provider_output(
            output,
            {
                "question": "目标日付费金额为什么变化？",
                "run_mode": "live",
            },
            registry,
        )

    output["analysis_requirements"]["claim_intent_roles"][
        "observed_activity"
    ] = "llm_candidate"
    workflow._validate_business_intent_provider_output(
        output,
        {
            "question": "目标日付费金额为什么变化？",
            "run_mode": "live",
        },
        registry,
    )


@pytest.mark.parametrize(
    ("required_claim_intents", "candidate_claim_intents"),
    (
        (("comparative_change",), ("observed_activity",)),
        (
            ("comparative_change",),
            ("comparative_change", "baseline_stability"),
        ),
    ),
)
def test_compiler_rejects_open_or_overlapping_claim_role_partition(
    required_claim_intents,
    candidate_claim_intents,
):
    with pytest.raises(ValueError, match="claim_intent_role_partition_invalid"):
        _compile(
            accepted_capabilities=("compare_periods",),
            claim_intents=("comparative_change", "baseline_stability"),
            required_claim_intents=required_claim_intents,
            candidate_claim_intents=candidate_claim_intents,
        )


def test_route_cards_expose_one_runtime_claim_contract_to_deepseek():
    from bi_agent.runtime import langgraph_workflow as workflow

    compare = next(
        card
        for card in workflow._route_capability_cards()
        if card["capability_id"] == "compare_periods"
    )

    assert compare["allowed_claim_types"] == ["comparative_change"]
    assert compare["supported_claim_types"] == ["comparative_change"]


def test_route_prompt_keeps_user_direction_as_a_pre_query_hypothesis():
    spec = build_prompt("analysis_route_plan", {"intent": _intent()})
    prompt = "\n".join(message["content"] for message in spec.messages)

    assert "Treat a user-stated increase or decrease as a hypothesis" in prompt
    assert "The first comparison verifies direction" in prompt
