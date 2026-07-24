from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from bi_agent.runtime.analysis_performance import (
    AnalysisPerformanceContractError,
    AnalysisPerformancePolicy,
    build_analysis_performance_profile,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.conversation.agent_core import _record_analysis_performance_profile
from bi_agent.conversation.store import InMemoryConversationStore


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_BINDINGS = ROOT / "contracts" / "runtime" / "clickhouse-analysis-bindings.yaml"


def event(node: str, duration_ms: float) -> dict:
    return {
        "node": node,
        "attempt": 1,
        "status": "completed",
        "label": node,
        "llm": node in {"understand_business_intent", "compile_authoritative_plan"},
        "started_at": "2026-07-23T00:00:00+00:00",
        "finished_at": "2026-07-23T00:00:01+00:00",
        "duration_ms": duration_ms,
    }


def test_runtime_contract_exposes_audit_only_depth_preserving_performance_policy():
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)

    policy = registry.analysis_performance_policy

    assert policy.enforcement == "audit_only"
    assert policy.breach_action == "record_and_continue"
    assert policy.depth_protection == "preserve_required_coverage_and_verification"
    assert policy.full_factor_p50_target_ms == 300_000
    assert policy.full_factor_p95_target_ms == 480_000


def test_profile_maps_workflow_nodes_to_business_stages_and_ranks_bottlenecks():
    policy = AnalysisPerformancePolicy.from_contract(
        RuntimeContractRegistry.from_path(RUNTIME_BINDINGS).analysis_performance_policy.contract_payload()
    )
    profile = build_analysis_performance_profile(
        run_id="run-1",
        checkpoint_events=(
            event("understand_business_intent", 20_000),
            event("compile_authoritative_plan", 70_000),
            event("execute_capability_dag", 110_000),
            event("settle_claim_authority", 250_000),
            event("compose_claim_aware_narrative", 300_000),
            event("deliver_publication", 5_000),
        ),
        policy=policy,
        capability_substages=(
            {
                "stage": "query_execution",
                "operation": "query-1",
                "duration_ms": 900.0,
                "input_bytes": 2048,
            },
        ),
        provider_call_audits=(
            {
                "stage": "narrative",
                "audit": {
                    "task": "single_authority_narrative_writer",
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "model_tier": "critical",
                    "thinking": "enabled",
                    "duration_ms": 120_000,
                    "attempt_count": 2,
                    "input_bytes": 200_000,
                    "output_bytes": 8_000,
                    "usage": {
                        "prompt_tokens": 60_000,
                        "completion_tokens": 8_000,
                    },
                    "reasoning_content_present": True,
                    "attempt_failures": (
                        {
                            "attempt": 1,
                            "failure_code": "provider_output_invalid",
                            "duration_ms": 100_000,
                            "raw_response_bytes": 7_000,
                            "usage": {
                                "prompt_tokens": 60_000,
                                "completion_tokens": 7_000,
                            },
                            "reasoning_content_present": True,
                        },
                    ),
                },
            },
        ),
    )

    assert profile.schema_version == "analysis-performance-profile.v2"
    assert profile.enforcement == "audit_only"
    assert profile.total_observed_duration_ms == 755_000
    assert profile.budget_status == "breached"
    assert profile.stage_observations[0].stage == "intent"
    assert profile.bottlenecks[0].stage == "narrative"
    assert profile.bottlenecks[1].stage == "claim_authority"
    assert profile.capability_substages[0].input_bytes == 2048
    assert profile.provider_totals.call_count == 1
    assert profile.provider_totals.attempt_count == 2
    assert profile.provider_totals.retry_count == 1
    assert profile.provider_totals.total_duration_ms == 220_000
    assert profile.provider_totals.retry_duration_ms == 100_000
    assert profile.provider_totals.total_input_bytes == 400_000
    assert profile.provider_totals.total_output_bytes == 15_000
    assert profile.provider_totals.prompt_tokens == 120_000
    assert profile.provider_totals.completion_tokens == 15_000
    assert profile.provider_calls[0].failure_codes == (
        "provider_output_invalid",
    )
    assert profile.profile_ref.startswith("analysis-performance-profile:sha256:")


def test_performance_budget_breach_is_observable_and_never_changes_run_status():
    policy = AnalysisPerformancePolicy.from_contract(
        RuntimeContractRegistry.from_path(RUNTIME_BINDINGS).analysis_performance_policy.contract_payload()
    )
    profile = build_analysis_performance_profile(
        run_id="run-1",
        checkpoint_events=(event("compose_claim_aware_narrative", 900_000),),
        policy=policy,
    )

    payload = profile.to_dict()
    assert payload["budget_status"] == "breached"
    assert payload["breach_action"] == "record_and_continue"
    assert "run_status" not in payload
    assert "customer_payload" not in payload


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.__setitem__("enforcement", "hard_stop"),
        lambda value: value.__setitem__("breach_action", "truncate"),
        lambda value: value.__setitem__("depth_protection", "drop_optional_factors"),
        lambda value: value["stage_targets_ms"].__setitem__("narrative", 0),
    ),
)
def test_performance_policy_rejects_depth_reducing_or_invalid_contracts(mutation):
    payload = deepcopy(
        RuntimeContractRegistry.from_path(RUNTIME_BINDINGS).analysis_performance_policy.contract_payload()
    )
    mutation(payload)

    with pytest.raises(AnalysisPerformanceContractError):
        AnalysisPerformancePolicy.from_contract(payload)


def test_profile_rejects_malformed_checkpoint_or_substage_events():
    policy = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS).analysis_performance_policy
    with pytest.raises(AnalysisPerformanceContractError, match="checkpoint_event_invalid"):
        build_analysis_performance_profile(
            run_id="run-1",
            checkpoint_events=({"node": "plan", "duration_ms": -1},),
            policy=policy,
        )
    with pytest.raises(AnalysisPerformanceContractError, match="capability_substage_invalid"):
        build_analysis_performance_profile(
            run_id="run-1",
            checkpoint_events=(event("execute_capability_dag", 1),),
            policy=policy,
            capability_substages=(
                {"stage": "query_execution", "operation": "q", "duration_ms": 1},
            ),
        )
    with pytest.raises(
        AnalysisPerformanceContractError,
        match="provider_call_audit_invalid",
    ):
        build_analysis_performance_profile(
            run_id="run-1",
            checkpoint_events=(event("execute_capability_dag", 1),),
            policy=policy,
            provider_call_audits=(
                {
                    "stage": "narrative",
                    "audit": {
                        "task": "writer",
                        "provider": "deepseek",
                        "model": "model",
                    },
                },
            ),
        )


def test_profile_is_written_only_to_waje_audit():
    store = InMemoryConversationStore()
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)

    _record_analysis_performance_profile(
        store=store,
        registry=registry,
        run_id="run-1",
        thread_id="thread-1",
        topic_id="topic-1",
        checkpoint_events=(
            {
                **event("execute_capability_dag", 120_000),
                "capability_substages": (
                    {
                        "stage": "query_execution",
                        "operation": "query-1",
                        "duration_ms": 10,
                        "input_bytes": 100,
                    },
                ),
            },
        ),
    )

    recorded = store.audit_events[-1]
    assert recorded["event_type"] == "analysis_performance_profile_recorded"
    assert recorded["payload"]["enforcement"] == "audit_only"
    assert recorded["payload"]["capability_substages"][0]["input_bytes"] == 100
    assert recorded["payload"]["provider_totals"]["call_count"] == 0
    assert "customer_payload" not in recorded["payload"]
    assert store.runs == {}


def test_performance_audit_failure_cannot_change_business_execution():
    class UnavailableAuditStore:
        def add_audit_event(self, *_args, **_kwargs):
            raise RuntimeError("audit_unavailable")

    _record_analysis_performance_profile(
        store=UnavailableAuditStore(),
        registry=RuntimeContractRegistry.from_path(RUNTIME_BINDINGS),
        run_id="run-1",
        thread_id="thread-1",
        topic_id="topic-1",
        checkpoint_events=(event("deliver_publication", 1),),
    )
