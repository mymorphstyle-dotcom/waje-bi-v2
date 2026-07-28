from __future__ import annotations

import json
from typing import Any

import pytest

from bi_agent.runtime.llm_client import LLMConfigurationError, LLMResult
from bi_agent.runtime.recorded_plan_proposal import RecordedPlanProposalClient


def _messages(
    *,
    run_id: str,
    intent_id: str,
    decision_id: str,
    question: str,
    actual_as_of: str,
    snapshot_ref: str = "snapshot:one",
) -> tuple[dict[str, str], ...]:
    payload = {
        "intent_revision": {
            "schema_version": "intent-revision.v3",
            "run_attempt_id": run_id,
            "intent_revision_id": intent_id,
            "content_digest": "a" * 64,
            "original_user_text": "某指标为什么上涨？",
            "business_summary": "你希望分析某指标上涨的业务驱动。",
            "goal_bindings": [{"goal_id": "explain_change", "role": "primary"}],
            "target_metric_refs": ["metric:one"],
            "scope": {"scope_type": "full_sample", "filters": []},
            "time_spec": {"kind": "date", "target": "2026-06-01"},
            "comparison_spec": {
                "kind": "decision_slot",
                "slot_id": "comparison_baseline",
            },
            "direction_premise": "user_hypothesis_positive",
            "requested_analysis_axes": ["axis:one"],
            "requested_factor_refs": [],
            "desired_decisions": [
                {"decision_kind": "direction", "target_ref": "metric:one"}
            ],
            "ambiguity_slots": [
                {
                    "slot_id": "comparison_baseline",
                    "slot_kind": "baseline",
                    "materiality": "material",
                    "allowed_value_refs": ["previous_day"],
                    "status": "unresolved",
                    "question": question,
                }
            ],
            "source_spans": [
                {
                    "field": "original_user_text",
                    "start": 0,
                    "end": 9,
                    "text": "某指标为什么上涨？",
                }
            ],
        },
        "active_decisions": [
            {
                "decision_id": decision_id,
                "intent_revision_id": intent_id,
                "content_digest": "b" * 64,
                "slot_id": "comparison_baseline",
                "status": "user_confirmed",
                "source": "user",
                "materiality": "material",
                "option_id": "comparison_baseline.previous_day",
                "value": {"baseline_id": "previous_day"},
                "affected_plan_fields": [
                    "baseline_refs",
                    "resolved_window_refs",
                ],
            }
        ],
        "authority_context": {
            "run_attempt_id": run_id,
            "authority_context_ref": f"authority:{run_id}",
            "content_digest": "c" * 64,
            "actual_as_of": actual_as_of,
            "contract_versions": {"runtime": "1"},
            "dataset_coverage": [
                {
                    "dataset_id": "dataset:one",
                    "availability": "claim_ready",
                    "release_ref": "release:one",
                    "snapshot_refs": [snapshot_ref],
                    "limitation_ref": None,
                }
            ],
            "release_refs": ["release:one"],
            "snapshot_refs": [snapshot_ref],
        },
        "goal_contracts": [{"goal_id": "explain_change"}],
        "analysis_axis_catalog": [{"axis_id": "axis:one"}],
        "capability_summaries": [{"capability_id": "capability:one"}],
    }
    return (
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": (
                "Inputs are delimited JSON.\n<input_json>\n"
                + json.dumps(payload, sort_keys=True)
                + "\n</input_json>\nRules"
            ),
        },
    )


class _Provider:
    supports_model_tier = True
    supports_thinking_mode = True
    durable_max_attempts = 3
    provider = "deepseek"
    model = "deepseek-v4-flash"
    critical_model = "deepseek-v4-pro"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        return LLMResult(output={"answer": "live"}, audit={"task": kwargs["task"]})


class _Connection:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def execute(self, *_args: Any, **_kwargs: Any) -> "_Connection":
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return [
            {
                "accepted_attempt_ref": "provider-call-attempt:source",
                "output_payload": self.payload,
            }
        ]


def _source_payload(messages: tuple[dict[str, str], ...]) -> dict[str, Any]:
    output = {
        "issue_tree": [
            {
                "issue_id": "root",
                "parent_issue_id": None,
                "question": "为什么上涨？",
                "target_claim_kind": "comparative_change",
            }
        ],
        "auxiliary_axes": [],
        "hypotheses": [],
        "priority_proposals": [],
        "assumption_proposals": [],
    }
    raw = json.dumps(output, ensure_ascii=False)
    return {
        "output": output,
        "audit": {
            "task": "single_authority_plan_proposal",
            "prompt_version": "plan.v1",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "model_tier": "critical",
            "thinking": "disabled",
            "messages": list(messages),
            "raw_response_content": raw,
            "structured_output": output,
            "output_hash": "source-output-hash",
            "base_url_hash": "deepseek-base-url-hash",
        },
    }


def test_recorded_plan_replays_across_volatile_authority_ids() -> None:
    source_messages = _messages(
        run_id="run-source",
        intent_id="intent-source",
        decision_id="decision-source",
        question="请选择比较基线：",
        actual_as_of="2026-07-24T01:00:00Z",
    )
    current_messages = _messages(
        run_id="run-current",
        intent_id="intent-current",
        decision_id="decision-current",
        question="前一天还是近七天？",
        actual_as_of="2026-07-24T02:00:00Z",
    )
    provider = _Provider()
    client = RecordedPlanProposalClient(
        provider,
        connection=_Connection(_source_payload(source_messages)),
        source_run_id="run-source",
    )

    result = client.invoke_json(
        task="single_authority_plan_proposal",
        prompt_version="plan.v1",
        messages=current_messages,
        required_keys=(
            "issue_tree",
            "auxiliary_axes",
            "hypotheses",
            "priority_proposals",
            "assumption_proposals",
        ),
        output_validator=lambda output: None,
        model_tier="critical",
        thinking="disabled",
    )

    assert provider.calls == []
    assert result.output["issue_tree"][0]["issue_id"] == "root"
    assert result.audit["provider"] == "deepseek"
    assert result.audit["model"] == "deepseek-v4-pro"
    assert result.audit["usage"]["total_tokens"] == 0
    assert result.audit["recorded_plan_replay"] == {
        "source_run_id": "run-source",
        "source_attempt_ref": "provider-call-attempt:source",
        "source_output_hash": "source-output-hash",
    }


def test_recorded_plan_rejects_snapshot_drift() -> None:
    source_messages = _messages(
        run_id="run-source",
        intent_id="intent-source",
        decision_id="decision-source",
        question="请选择比较基线：",
        actual_as_of="2026-07-24T01:00:00Z",
    )
    current_messages = _messages(
        run_id="run-current",
        intent_id="intent-current",
        decision_id="decision-current",
        question="请选择比较基线：",
        actual_as_of="2026-07-24T02:00:00Z",
        snapshot_ref="snapshot:two",
    )
    client = RecordedPlanProposalClient(
        _Provider(),
        connection=_Connection(_source_payload(source_messages)),
        source_run_id="run-source",
    )

    with pytest.raises(LLMConfigurationError, match="recorded_plan_input_mismatch"):
        client.invoke_json(
            task="single_authority_plan_proposal",
            prompt_version="plan.v1",
            messages=current_messages,
            required_keys=("issue_tree",),
            model_tier="critical",
            thinking="disabled",
        )


def test_recorded_plan_delegates_non_plan_tasks() -> None:
    provider = _Provider()
    client = RecordedPlanProposalClient(
        provider,
        connection=_Connection({}),
        source_run_id="run-source",
    )

    result = client.invoke_json(
        task="single_authority_intent",
        prompt_version="intent.v1",
        messages=({"role": "user", "content": "question"},),
        required_keys=("answer",),
        model_tier="critical",
        thinking="disabled",
    )

    assert result.output == {"answer": "live"}
    assert len(provider.calls) == 1
