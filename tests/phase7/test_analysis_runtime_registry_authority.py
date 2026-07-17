from __future__ import annotations

from unittest.mock import patch

from bi_agent.runtime.analysis_runtime import AnalysisRuntimeRequest
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase7.test_analysis_runtime_reuse import (
    _candidate,
    _proposal,
    _publish_source,
    _runtime_fixture,
    _source_request,
)


def _drifted_cached_registry() -> RuntimeContractRegistry:
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    registry._payload["metrics"]["paid_amount"]["expression"] = (
        "sum(paid_amount_ngn) + 0"
    )
    return registry


def test_fresh_execution_uses_analysis_runtime_registry_authority() -> None:
    runtime, provider, _, topic_id, _ = _runtime_fixture()

    with patch(
        "bi_agent.runtime.clickhouse_query_compiler._runtime_registry",
        return_value=_drifted_cached_registry(),
    ):
        result = runtime.execute(_source_request("run-registry-fresh", topic_id))

    assert provider.calls == 1
    assert result.status == "ready"
    assert result.query_results[0].execution_status == "succeeded"


def test_reuse_materialization_uses_analysis_runtime_registry_authority() -> None:
    runtime, provider, store, topic_id, signed = _runtime_fixture()
    source = runtime.execute(_source_request("run-registry-source", topic_id))
    candidate = _candidate(runtime, source, signed)
    _publish_source(runtime, store, topic_id, source, candidate)

    with patch(
        "bi_agent.runtime.clickhouse_query_compiler._runtime_registry",
        return_value=_drifted_cached_registry(),
    ):
        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-registry-reuse",
                topic_id=topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                reuse_candidates=(candidate,),
            )
        )

    assert provider.calls == 1
    assert current.status == "clarify"
    assert current.query_results[0].execution_status == "succeeded"
    assert current.reuse_decisions[0]["decision"] == "reuse"
    assert current.reuse_decisions[0]["reason"] == (
        "validated_authoritative_query_chain"
    )
