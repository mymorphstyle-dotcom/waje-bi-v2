from __future__ import annotations

from bi_agent.runtime.llm_prompts import (
    CLAIM_COVERAGE_EXPANSION_PROMPT_VERSION,
    SINGLE_AUTHORITY_PLAN_PATCH_PROMPT_VERSION,
    build_prompt,
    validate_prompt_specs,
)


def _user_prompt(task: str) -> str:
    spec = build_prompt(task, {"contract_check": True})
    return spec.messages[1]["content"]


def test_claim_coverage_decision_prompt_has_exact_typed_output() -> None:
    spec = build_prompt(
        "claim_coverage_expansion_decision",
        {
            "unresolved_obligation_ids": ["obligation:change"],
            "admissible_routes": [{"axis_id": "dimension_breakdown"}],
        },
    )

    assert spec.prompt_version == CLAIM_COVERAGE_EXPANSION_PROMPT_VERSION
    assert spec.required_keys == ("decision", "selected_axis_ids")
    assert "display_summary" not in spec.required_keys
    prompt = spec.messages[1]["content"]
    assert "Return exactly {decision, selected_axis_ids}" in prompt
    assert "non-empty, unique subset" in prompt
    assert "success_policy" in prompt
    assert "aggregate observation facts" in prompt
    assert "exploration_stop_policy" in prompt
    assert "evidence_present means evidence exists" in prompt
    assert "business_name, semantics, selection_policy" in prompt
    assert "maximum_claim_strength_by_obligation" in prompt
    assert "expected_value_projection" in prompt
    assert "incremental_capability_ids" in prompt
    assert "protected_incremental_capability_ids" in prompt
    assert "auxiliary_incremental_capability_ids" in prompt
    assert "estimated_budget_units" in prompt
    assert "estimated_auxiliary_budget_units" in prompt
    assert "remaining_auxiliary_budget_units" in prompt
    assert "Do not return reasons, confidence" in prompt


def test_plan_patch_prompt_is_full_successor_with_selected_axis_boundary() -> None:
    spec = build_prompt(
        "single_authority_plan_patch_proposal",
        {
            "source_plan_revision": {
                "analysis_axes": [{"axis_id": "change_validation"}]
            },
            "plan_patch": {"selected_axis_ids": ["dimension_breakdown"]},
        },
    )

    assert spec.prompt_version == SINGLE_AUTHORITY_PLAN_PATCH_PROMPT_VERSION
    assert spec.required_keys == (
        "issue_tree",
        "auxiliary_axes",
        "hypotheses",
        "priority_proposals",
        "assumption_proposals",
    )
    assert "display_summary" not in spec.required_keys
    prompt = spec.messages[1]["content"]
    assert "full successor proposal" in prompt
    assert "every newly introduced axis_id must be" in prompt
    assert "source plan or be selected by the PlanPatch" in prompt
    assert "analytical framing, emphasis, and business wording" in prompt


def test_claim_coverage_prompt_registry_is_complete() -> None:
    assert _user_prompt("claim_coverage_expansion_decision")
    assert _user_prompt("single_authority_plan_patch_proposal")
    assert validate_prompt_specs() == []
