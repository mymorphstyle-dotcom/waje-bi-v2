from types import SimpleNamespace

from bi_agent.runtime import langgraph_workflow as workflow


def _state() -> dict:
    return {
        "request": {"allow_question_interrupt": False},
        "intent": {"target_metric": "paid_amount"},
        "compiled_graph": SimpleNamespace(
            mutations=SimpleNamespace(accepted_graph=("compare_periods",))
        ),
        "evidence_brief": {},
        "diagnostic_insights": {
            "diagnostic_sufficiency": {"status": "bounded", "next_routes": []}
        },
        "llm_calls": [
            {
                "task": "next_action",
                "provider": "openai",
                "failure_code": "llm_narrative_invalid:decision_summary",
                "attempt_failures": [
                    {
                        "structured_output": {
                            "next_action": "synthesize_answer",
                            "decision_summary": "当前诊断状态为 bounded。",
                        }
                    }
                ],
            }
        ],
    }


def test_next_action_narrative_failure_uses_local_business_decision_and_keeps_raw_audit(
    monkeypatch,
):
    state = _state()
    raw_audit = state["llm_calls"][0]

    def fail_writer(*_args, **_kwargs):
        raise workflow.WorkflowFailure(
            "llm_narrative_invalid:decision_summary",
            failure_type="llm",
        )

    monkeypatch.setattr(workflow, "_invoke_llm", fail_writer)
    monkeypatch.setattr(workflow, "_evidence_supports_bounded_answer", lambda _state: True)
    monkeypatch.setattr(workflow, "_pattern_has_negative_answer_evidence", lambda _state: False)
    monkeypatch.setattr(workflow, "_evidence_has_terminal_business_boundary", lambda _state: False)

    output = workflow._decide_next_action(state)

    assert output["next_action"]["next_action"] == "synthesize_answer"
    assert output["next_action"]["local_narrative_fallback"] is True
    assert (
        output["next_action"]["fallback_reason"]
        == "llm_narrative_invalid:decision_summary"
    )
    assert output["llm_calls"][0] is raw_audit
    assert output["llm_calls"][-1]["provider"] == "local_deterministic"
    assert "bounded" not in output["next_action"]["decision_summary"]


def test_next_action_keeps_provider_action_and_projects_private_status_to_business_text(
    monkeypatch,
):
    state = _state()
    invoke_options = {}

    def provider_output(*_args, **kwargs):
        invoke_options.update(kwargs)
        return {
            "next_action": "synthesize_answer",
            "decision_summary": "当前诊断状态为 bounded，可以生成答案。",
            "display_summary": "当前诊断状态为 bounded，可以生成答案。",
        }

    monkeypatch.setattr(workflow, "_invoke_llm", provider_output)

    output = workflow._decide_next_action(state)

    assert invoke_options["defer_narrative_validation"] is True
    assert output["next_action"]["next_action"] == "synthesize_answer"
    assert output["next_action"]["provider_narrative_audit_only"] is True
    assert "bounded" not in output["next_action"]["decision_summary"]
    assert "形成答案" in output["next_action"]["decision_summary"]


def test_next_action_local_fallback_continues_a_pending_material_route(monkeypatch):
    state = _state()
    state["diagnostic_insights"]["diagnostic_sufficiency"] = {
        "status": "continue",
        "next_routes": [{"route_id": "localize_primary_driver"}],
    }

    monkeypatch.setattr(
        workflow,
        "_pending_diagnostic_route_ids",
        lambda _state: ("localize_primary_driver",),
    )

    decision = workflow._deterministic_next_action(
        state,
        fallback_reason="provider_unavailable",
    )

    assert decision["next_action"] == "continue_evidence"
    assert decision["diagnostic_route"] == "localize_primary_driver"


def test_next_action_local_fallback_keeps_terminal_hard_boundary(monkeypatch):
    state = _state()
    monkeypatch.setattr(workflow, "_pending_diagnostic_route_ids", lambda _state: ())
    monkeypatch.setattr(workflow, "_evidence_supports_bounded_answer", lambda _state: False)
    monkeypatch.setattr(workflow, "_pattern_has_negative_answer_evidence", lambda _state: False)
    monkeypatch.setattr(workflow, "_evidence_has_terminal_business_boundary", lambda _state: True)

    decision = workflow._deterministic_next_action(
        state,
        fallback_reason="provider_unavailable",
    )

    assert decision["next_action"] == "degrade"
    assert "不能发布" in decision["decision_summary"]
