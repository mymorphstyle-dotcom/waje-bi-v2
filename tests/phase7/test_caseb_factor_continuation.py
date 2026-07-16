from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.runtime.answer_package import (
    AuthorityFact,
    _claim_authority_facts,
    _claim_authority_errors,
    _partial_claim_delivery_text,
    _project_claim_from_authority,
    reproject_answer_package_from_persisted_authority,
    verify_answer_package,
)
from bi_agent.runtime import analysis_runtime as analysis_runtime_module
from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    ResolvedWindow,
    query_contract_signature,
)
from bi_agent.runtime import capability_execution as capability_execution_module
from bi_agent.runtime.capability_execution import BoundCapabilityInput
from bi_agent.runtime.exploration_budget import default_budget
from bi_agent.runtime.langgraph_workflow import (
    _answer_synthesis_context,
    _default_claim_from_primary_evidence,
    _execute_capabilities,
    _final_business_summary_payload,
    _final_summary_needs_display_repair,
    _hard_verify_answer,
    _key_findings_sentence,
    _local_final_answer_hard_blockers,
    _normalize_claim_numbers,
    _production_bound_input,
    _repair_answer,
    _route_after_hard_verify,
    _route_after_semantic_audit,
    _verified_claims,
)
from bi_agent.runtime.llm_prompts import build_prompt
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase7.test_core_driver_capability_binding import (
    CORE_METRICS,
    _component_query,
)


def _state(*capabilities: str) -> dict:
    return {
        "request": {
            "run_mode": "fixture",
            "role": "analyst",
            "runtime_rows_by_intent": {
                "daily_metric_baselines": (
                    {
                        "period": "baseline-window",
                        "group": "previous_day",
                        "amount": 100.0,
                    },
                    {
                        "period": "target-window",
                        "group": "target",
                        "amount": 120.0,
                    },
                ),
                "data_quality_probe": (
                    {
                        "window_id": "previous_day",
                        "window_role": "baseline",
                        "observation_key": "baseline-window",
                        "source_row_count": 10,
                        "paid_amount": 100.0,
                    },
                    {
                        "window_id": "target_day",
                        "window_role": "target",
                        "observation_key": "target-window",
                        "source_row_count": 12,
                        "paid_amount": 120.0,
                    },
                ),
            },
        },
        "run_id": "run-factor-continuation",
        "sql_hash": "",
        "budget_state": default_budget("ordinary"),
        "compiled_graph": SimpleNamespace(
            mutations=SimpleNamespace(accepted_graph=capabilities)
        ),
        "intent": {
            "question_family": "paid_amount_change_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "pattern_params": {
                "group_key": "group",
                "target_group": "target",
                "baseline_group": "previous_day",
            },
            "scope": "full_sample",
            "time_window": "target-window",
            "target_claim": "解释目标日付费金额上涨的原因",
            "baseline": {"label": "前一天"},
            "target": {"label": "目标日"},
        },
    }


def test_omittable_capability_gap_degrades_ready_siblings_without_clarification():
    result = analysis_runtime_module.AnalysisRuntimeResult(
        analysis_contract=object(),
        query_contracts=(),
        query_results=(object(),),
        completeness_reports=(),
        capability_plans=(
            SimpleNamespace(
                capability_id="formula_decompose",
                degradation_policy={"missing_required_input": "block_claim"},
            ),
            SimpleNamespace(
                capability_id="user_mix_contribution",
                degradation_policy={"missing_required_input": "omit_path"},
            ),
        ),
        bound_capability_inputs={
            "formula_decompose": SimpleNamespace(status="ready"),
            "user_mix_contribution": SimpleNamespace(status="blocked"),
        },
        repair_decisions=(),
        typed_gaps=(
            {
                "gap_type": "contract_partial",
                "requires_clarification": False,
                "affected_capabilities": ("user_mix_contribution",),
                "affected_claim_types": ("contribution",),
                "diagnostic_context": {
                    "analysis_role": "auxiliary",
                    "degradation_action": "omit_path",
                },
            },
        ),
        persistence_records={},
    )

    assert result.status == "degraded"


def test_user_required_omittable_gap_still_clarifies_with_ready_sibling():
    result = analysis_runtime_module.AnalysisRuntimeResult(
        analysis_contract=object(),
        query_contracts=(),
        query_results=(object(),),
        completeness_reports=(),
        capability_plans=(
            SimpleNamespace(
                capability_id="formula_decompose",
                degradation_policy={"missing_required_input": "block_claim"},
            ),
            SimpleNamespace(
                capability_id="user_mix_contribution",
                degradation_policy={"missing_required_input": "omit_path"},
            ),
        ),
        bound_capability_inputs={
            "formula_decompose": SimpleNamespace(status="ready"),
            "user_mix_contribution": SimpleNamespace(status="blocked"),
        },
        repair_decisions=(),
        typed_gaps=(
            {
                "gap_type": "contract_partial",
                "requires_clarification": True,
                "affected_capabilities": ("user_mix_contribution",),
                "affected_claim_types": ("contribution",),
                "diagnostic_context": {
                    "analysis_role": "required",
                    "degradation_action": "omit_path",
                },
            },
        ),
        persistence_records={},
    )

    assert result.status == "clarify"


def test_blocking_capability_gap_still_clarifies_with_ready_sibling():
    result = analysis_runtime_module.AnalysisRuntimeResult(
        analysis_contract=object(),
        query_contracts=(),
        query_results=(object(),),
        completeness_reports=(),
        capability_plans=(
            SimpleNamespace(
                capability_id="formula_decompose",
                degradation_policy={"missing_required_input": "block_claim"},
            ),
            SimpleNamespace(
                capability_id="required_context",
                degradation_policy={"missing_required_input": "block_claim"},
            ),
        ),
        bound_capability_inputs={
            "formula_decompose": SimpleNamespace(status="ready"),
            "required_context": SimpleNamespace(status="blocked"),
        },
        repair_decisions=(),
        typed_gaps=(
            {
                "gap_type": "contract_partial",
                "requires_clarification": True,
                "affected_capabilities": ("required_context",),
                "affected_claim_types": ("business_object_candidate_impact",),
            },
        ),
        persistence_records={},
    )

    assert result.status == "clarify"


def test_blocked_auxiliary_binding_gets_branch_scoped_terminal_record():
    claim_type = "segment_contribution_or_mix_shift"
    plan = SimpleNamespace(
        capability_id="generic_dimension_screen",
        capability_contract_signature="capability-signature",
        supported_claim_types=(claim_type,),
        required_input_slots=(
            SimpleNamespace(
                query_contract_refs=("query:dimension",),
                validation_query_contract_refs=("query:overall",),
            ),
        ),
        optional_input_slots=(),
    )
    bound = SimpleNamespace(
        status="blocked",
        binding_manifest_ref="",
        reasons=("shared_binding_invalid",),
    )
    records = analysis_runtime_module._auxiliary_terminal_records_for_unbound_results(
        capability_plans=(plan,),
        bound_capability_inputs={plan.capability_id: bound},
        query_results=(
            SimpleNamespace(
                query_contract_ref="query:overall",
                result_ref="result:overall",
            ),
            SimpleNamespace(
                query_contract_ref="query:dimension",
                result_ref="result:dimension",
            ),
        ),
        candidate_claim_intents=(claim_type,),
    )

    assert records == (
        {
            "failed_signature": "capability-signature",
            "action": "quarantine_auxiliary_results",
            "reason": "shared_binding_invalid",
            "capability_id": "generic_dimension_screen",
            "analysis_role": "auxiliary",
            "affected_claim_types": (claim_type,),
            "query_contract_refs": ("query:dimension", "query:overall"),
            "result_refs": ("result:dimension", "result:overall"),
            "failure_stage": "capability_binding",
            "publication_authority": "none",
        },
    )


def test_degraded_auxiliary_binding_quarantines_only_unbound_branch_results():
    claim_type = "segment_contribution_or_mix_shift"
    plan = SimpleNamespace(
        capability_id="generic_dimension_screen",
        capability_contract_signature="capability-signature",
        supported_claim_types=(claim_type,),
        required_input_slots=(
            SimpleNamespace(
                query_contract_refs=("query:available",),
                validation_query_contract_refs=("query:overall",),
            ),
            SimpleNamespace(
                query_contract_refs=("query:failed",),
                validation_query_contract_refs=("query:overall",),
            ),
        ),
        optional_input_slots=(),
    )
    bound = SimpleNamespace(
        status="degraded",
        binding_manifest_ref="binding:dimension-screen",
        result_refs=("result:available",),
        validation_result_refs=("result:overall",),
        reasons=("missing_required_slot:failed_profile",),
    )

    records = analysis_runtime_module._auxiliary_terminal_records_for_unbound_results(
        capability_plans=(plan,),
        bound_capability_inputs={plan.capability_id: bound},
        query_results=(
            SimpleNamespace(
                query_contract_ref="query:overall",
                result_ref="result:overall",
            ),
            SimpleNamespace(
                query_contract_ref="query:available",
                result_ref="result:available",
            ),
            SimpleNamespace(
                query_contract_ref="query:failed",
                result_ref="result:failed",
            ),
        ),
        candidate_claim_intents=(claim_type,),
    )

    assert records == (
        {
            "failed_signature": "capability-signature",
            "action": "quarantine_auxiliary_results",
            "reason": "missing_required_slot:failed_profile",
            "capability_id": "generic_dimension_screen",
            "analysis_role": "auxiliary",
            "affected_claim_types": (claim_type,),
            "query_contract_refs": ("query:failed",),
            "result_refs": ("result:failed",),
            "failure_stage": "capability_binding",
            "publication_authority": "none",
        },
    )


def test_persistence_repair_records_include_auxiliary_terminal_closure():
    terminal = {
        "failed_signature": "capability-signature",
        "action": "quarantine_auxiliary_results",
        "reason": "binding_invalid",
        "capability_id": "generic_dimension_screen",
        "analysis_role": "auxiliary",
        "affected_claim_types": ("segment_contribution_or_mix_shift",),
        "query_contract_refs": ("query:dimension",),
        "result_refs": ("result:dimension",),
        "failure_stage": "capability_binding",
        "publication_authority": "none",
    }

    records = analysis_runtime_module._persistence_repair_records(
        analysis_contract_ref="analysis:1",
        repair_decisions=(
            SimpleNamespace(
                failed_signature="query-signature",
                action="degrade",
                reason="query_incomplete",
            ),
        ),
        auxiliary_terminal_records=(terminal,),
    )

    assert records[0] == {
        "attempt_ref": "repair:analysis:1:1",
        "failed_signature": "query-signature",
        "action": "degrade",
        "reason": "query_incomplete",
    }
    assert records[1] == {
        "attempt_ref": "repair:analysis:1:2",
        **terminal,
    }


def test_compare_periods_uses_canonical_claim_type_when_business_target_is_narrative():
    state = _state("compare_periods")
    captured = []

    def execute(request):
        captured.append(request)
        return {
            "evidence_ref": "compare-periods:evidence",
            "capability_id": request.capability_id,
            "typed_payload": {},
            "result_refs": (),
        }

    with patch(
        "bi_agent.runtime.langgraph_workflow.execute_capability",
        side_effect=execute,
    ):
        _execute_capabilities(state)

    assert len(captured) == 1
    assert captured[0].target_claim == "解释目标日付费金额上涨的原因"
    assert captured[0].claim_type == "comparative_change"


def test_data_quality_uses_required_fields_from_accepted_capability_plan():
    state = _state("data_quality_profile")
    required_fields = (
        "window_id",
        "window_role",
        "observation_key",
        "source_row_count",
    )
    state["request"]["capability_execution_plans"] = (
        CapabilityExecutionPlan(
            capability_id="data_quality_profile",
            capability_contract_ref="runtime#data_quality_profile",
            required_input_slots=(
                CapabilityInputSlot(
                    slot_id="data_quality_probe",
                    query_contract_refs=("query:data-quality",),
                    required=True,
                    accepted_completeness=("complete",),
                    required_fields=required_fields,
                    required_window_ids=("target_day",),
                ),
            ),
            optional_input_slots=(),
            merge_strategy="single",
            minimum_readiness={"required_slots": "all"},
            degradation_policy={"missing_required_input": "report_contract_gap"},
            supported_evidence_types=("insufficient",),
            maximum_claim_strength="trust_boundary",
            supported_claim_types=("contract_coverage_and_trust_boundary",),
        ),
    )
    captured = []

    def execute(request):
        captured.append(request)
        return {
            "evidence_ref": "data-quality:evidence",
            "capability_id": request.capability_id,
            "typed_payload": {},
            "result_refs": (),
        }

    with patch(
        "bi_agent.runtime.langgraph_workflow.execute_capability",
        side_effect=execute,
    ):
        _execute_capabilities(state)

    assert len(captured) == 1
    assert captured[0].params["required_fields"] == required_fields


def test_data_quality_evidence_type_matches_runtime_capability_contract():
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    supported = tuple(
        registry.capability_inputs("data_quality_profile").get(
            "supported_evidence_types", ()
        )
    )
    result = data_quality_check(
        [
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "target-window",
                "source_row_count": 12,
            }
        ],
        required_fields=(
            "window_id",
            "window_role",
            "observation_key",
            "source_row_count",
        ),
    )

    assert result.evidence_type in supported
    assert "no_required_fields_checked" not in result.limitations


def test_production_consumer_keeps_authenticated_degraded_capability_executable():
    bound = object.__new__(BoundCapabilityInput)
    for field in BoundCapabilityInput.__annotations__:
        if field in {"rows_by_slot", "binding_manifest"}:
            value = {}
        elif field == "maximum_claim_strength_rank":
            value = 2
        elif field.endswith("s") or field.endswith("refs"):
            value = ()
        else:
            value = ""
        object.__setattr__(bound, field, value)
    object.__setattr__(bound, "capability_id", "driver_decomposition")
    object.__setattr__(bound, "status", "degraded")
    object.__setattr__(
        bound,
        "rows_by_slot",
        {
            "component_driver_scan": (
                {
                    "window_id": "previous_day",
                    "paid_amount": 100.0,
                    "paid_users": 10,
                },
                {
                    "window_id": "target_day",
                    "paid_amount": 120.0,
                    "paid_users": 11,
                },
            )
        },
    )
    object.__setattr__(
        bound,
        "reasons",
        ("missing_optional_slot:payment_success_scan",),
    )

    state = {
        "request": {
            "run_mode": "production",
            "runtime_rows_source": "analysis_runtime",
            "bound_capability_inputs": {"driver_decomposition": bound},
        }
    }
    with patch(
        "bi_agent.runtime.langgraph_workflow.validate_bound_capability_input",
        return_value="",
    ):
        selected, limitation = _production_bound_input(
            state,
            "driver_decomposition",
        )

    assert selected is bound
    assert limitation == ""


def _core_driver_state() -> dict:
    evidence = driver_decomposition(
        (
            {
                "period": "comparison",
                "group": "baseline",
                "amount": 100.0,
                "paid_users": 10.0,
                "paid_frequency": 2.0,
                "avg_order_amount": 5.0,
                "first_paid_users": 3.0,
            },
            {
                "period": "comparison",
                "group": "target",
                "amount": 180.0,
                "paid_users": 12.0,
                "paid_frequency": 2.5,
                "avg_order_amount": 6.0,
                "first_paid_users": 4.0,
            },
        )
    )
    item = asdict(evidence)
    item["evidence_ref"] = "driver_decomposition:case-b"
    return {
        "request": {"question": "付费金额为什么上涨？"},
        "intent": {
            "question_family": "paid_amount_change_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-05-31..2026-06-01",
            "target_claim": "解释目标日付费金额上涨的原因",
            "baseline": {"label": "2026-05-31"},
            "target": {"label": "2026-06-01"},
        },
        "evidence": [item],
    }


def test_driver_payload_projects_three_factor_claim_numbers_at_top_level():
    state = _core_driver_state()
    payload = state["evidence"][0]["typed_payload"]

    assert payload["primary_core_driver"] == "paid_frequency"
    assert payload["paid_users_contribution_share"] is not None
    assert payload["paid_frequency_contribution_share"] is not None
    assert payload["avg_order_amount_contribution_share"] is not None
    assert payload["core_reconciliation_status"] == "reconciled"
    assert payload["payment_success_assumption"]["observed"] is False


def test_answer_context_exposes_only_canonical_formula_claim_numbers_to_provider():
    state = _core_driver_state()

    context = _answer_synthesis_context(state)

    assert context["key_findings"]["numeric_facts"] == state["evidence"][0][
        "numeric_facts"
    ]
    assert set(context["key_findings"]["numeric_facts"]) == {
        "paid_users_contribution",
        "paid_users_contribution_share",
        "paid_frequency_contribution",
        "paid_frequency_contribution_share",
        "avg_order_amount_contribution",
        "avg_order_amount_contribution_share",
        "formula_contribution_total",
    }


def test_verifier_reads_formula_claim_numbers_from_canonical_numeric_facts():
    state = _core_driver_state()
    evidence = {
        **state["evidence"][0],
        "capability_id": "driver_decomposition",
        "claim_type": "formula_component_contribution",
        "claim_input_ready": True,
        "binding_manifest_ref": "binding:driver-decomposition",
        "supported_claim_types": ("formula_component_contribution",),
        "supported_evidence_types": ("accounting_contribution",),
    }
    claim = {
        "text": "三项核心因素的贡献已经完成核算。",
        "claim_type": "formula_component_contribution",
        "claim_strength": "quantified_contribution",
        "evidence_refs": (evidence["evidence_ref"],),
        "numbers": dict(evidence["numeric_facts"]),
    }

    with patch(
        "bi_agent.runtime.answer_package._claim_authority_errors",
        return_value=(),
    ):
        audit = verify_answer_package(
            draft_claims=(claim,),
            evidence=(evidence,),
            visible_limitations=evidence["limitations"],
            required_claim_intents=("formula_component_contribution",),
        )

    assert audit["status"] == "passed"
    assert audit["accepted_claim_indexes"] == (0,)
    assert not any(
        item.get("code") == "number_mismatch" for item in audit["errors"]
    )


def test_verifier_never_falls_back_to_diagnostic_typed_payload_numbers():
    evidence = {
        "evidence_ref": "driver_decomposition:number-boundary",
        "capability_id": "driver_decomposition",
        "claim_type": "formula_component_contribution",
        "claim_input_ready": True,
        "binding_manifest_ref": "binding:driver-decomposition",
        "evidence_type": "accounting_contribution",
        "supported_claim_types": ("formula_component_contribution",),
        "supported_evidence_types": ("accounting_contribution",),
        "wording_limit": "quantified",
        "limitations": (),
        "numeric_facts": {"formula_contribution_total": 80.0},
        "typed_payload": {
            "formula_contribution_total": 999.0,
            "formula_reconciliation_residual": 0.0,
        },
    }
    valid_claim = {
        "text": "三因素核算贡献合计为80。",
        "claim_type": "formula_component_contribution",
        "claim_strength": "quantified_contribution",
        "evidence_refs": (evidence["evidence_ref"],),
        "numbers": {"formula_contribution_total": 80.0},
    }
    diagnostic_claim = {
        **valid_claim,
        "numbers": {"formula_reconciliation_residual": 0.0},
    }

    with patch(
        "bi_agent.runtime.answer_package._claim_authority_errors",
        return_value=(),
    ):
        valid = verify_answer_package(
            draft_claims=(valid_claim,),
            evidence=(evidence,),
            visible_limitations=(),
        )
        diagnostic = verify_answer_package(
            draft_claims=(diagnostic_claim,),
            evidence=(evidence,),
            visible_limitations=(),
        )

    assert valid["accepted_claim_indexes"] == (0,)
    assert diagnostic["accepted_claim_indexes"] == ()
    assert any(
        item.get("code") == "number_mismatch"
        and item.get("field") == "formula_reconciliation_residual"
        for item in diagnostic["errors"]
    )


def test_answer_prompts_preserve_authoritative_numbers_and_units():
    synthesis = "\n".join(
        message["content"]
        for message in build_prompt(
            "answer_synthesis", {"businessContext": {}}
        ).messages
    )
    repair = "\n".join(
        message["content"]
        for message in build_prompt(
            "answer_repair",
            {"answerText": "", "businessContext": {}, "displayReview": {}},
        ).messages
    )

    assert "Copy every published number, direction, scope, and time boundary" in synthesis
    assert "Do not infer percentage units from numeric magnitude" in synthesis
    assert "Preserve their supported facts, numbers, scope, time window" in repair
    assert "Do not infer percentage units from numeric magnitude" in repair


def test_driver_payload_records_the_selected_comparison_basis():
    evidence = driver_decomposition(
        (
            {
                "period": "comparison",
                "group": "baseline",
                "amount": 100.0,
                "paid_users": 10.0,
                "paid_frequency": 2.0,
                "avg_order_amount": 5.0,
            },
            {
                "period": "comparison",
                "group": "target",
                "amount": 120.0,
                "paid_users": 12.0,
                "paid_frequency": 2.0,
                "avg_order_amount": 5.0,
            },
        ),
        target_window_id="target_day",
        baseline_window_id="previous_day",
    )

    payload = evidence.typed_payload
    assert payload["target_window_id"] == "target_day"
    assert payload["baseline_window_id"] == "previous_day"
    assert payload["target_value"] == pytest.approx(120.0)
    assert payload["baseline_value"] == pytest.approx(100.0)
    assert payload["amount_delta"] == pytest.approx(20.0)


def test_verifier_rejects_only_driver_claim_when_comparison_basis_disagrees():
    evidence = (
        {
            "evidence_ref": "compare_periods:case-b",
            "capability_id": "compare_periods",
            "evidence_type": "statistical_association",
            "wording_limit": "quantified",
            "typed_payload": {
                "metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-05-31..2026-06-01",
                "target_window_id": "target_day",
                "baseline_window_id": "previous_day",
                "absolute_change": 20.0,
            },
        },
        {
            "evidence_ref": "driver_decomposition:case-b",
            "capability_id": "driver_decomposition",
            "evidence_type": "accounting_contribution",
            "wording_limit": "quantified",
            "typed_payload": {
                "metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-05-31..2026-06-01",
                "target_window_id": "target_day",
                "baseline_window_id": "rolling_7_day_baseline",
                "amount_delta": 50.0,
            },
        },
    )
    claims = (
        {
            "claim_ref": "claim:comparison",
            "fact_refs": ("fact:comparison",),
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": ("compare_periods:case-b",),
            "numbers": {"absolute_change": 20.0},
        },
        {
            "claim_ref": "claim:driver",
            "fact_refs": ("fact:driver",),
            "claim_type": "formula_component_contribution",
            "claim_strength": "quantified_contribution",
            "evidence_refs": ("driver_decomposition:case-b",),
            "numbers": {"paid_users_contribution_share": 1.0},
        },
    )

    with patch(
        "bi_agent.runtime.answer_package._claim_authority_errors",
        return_value=(),
    ):
        audit = verify_answer_package(
            draft_claims=claims,
            evidence=evidence,
            visible_limitations=(),
        )

    assert audit["status"] == "degraded"
    assert audit["accepted_claim_indexes"] == (0,)
    assert audit["global_errors"] == []
    assert audit["required_claim_gaps"] == []
    assert {
        item["code"] for item in audit["claim_rejections"]
    } == {"comparison_basis_mismatch"}
    assert any(
        error.get("code") == "comparison_basis_mismatch"
        and error.get("claim_index") == 1
        for error in audit["errors"]
    )


def test_driver_answer_projection_reuses_only_its_selected_baseline():
    base = _component_query(CORE_METRICS)
    target = replace(base.resolved_windows[0], aggregation="daily_total")
    previous = replace(base.resolved_windows[1], aggregation="daily_total")
    rolling = ResolvedWindow(
        window_id="rolling_7_day_baseline",
        role="baseline",
        label="2026-05-25..2026-05-31",
        start_inclusive="2026-05-25",
        end_exclusive="2026-06-01",
        timezone="Africa/Lagos",
        aggregation="mean_of_complete_days",
        required_complete_days=7,
        source_watermark_requirement="2026-06-01",
    )
    windows = (target, rolling, previous)
    contract = replace(
        base,
        resolved_windows=windows,
        window_refs=tuple(window.window_id for window in windows),
        result_shape=replace(
            base.result_shape,
            required_window_ids=tuple(window.window_id for window in windows),
        ),
        contract_signature="",
    )
    contract = replace(
        contract,
        contract_signature=query_contract_signature(contract),
    )

    def row(window_id, role, day, amount, users, orders):
        return {
            "window_id": window_id,
            "window_role": role,
            "observation_key": day,
            "paid_amount": amount,
            "paid_users": users,
            "paid_orders": orders,
            "first_paid_users": users / 4,
            "paid_frequency": orders / users,
            "avg_order_amount": amount / orders,
        }

    rows = [
        row("target_day", "target", "2026-06-01", 180, 12, 30),
        row("previous_day", "baseline", "2026-05-31", 100, 10, 20),
    ]
    rows.extend(
        row(
            "rolling_7_day_baseline",
            "baseline",
            f"2026-05-{day:02d}",
            200 + day,
            20 + day,
            40 + day,
        )
        for day in range(25, 32)
    )
    result = SimpleNamespace(result_ref="result:driver", rows=tuple(rows))
    chain = SimpleNamespace(
        primary_results=(result,),
        query_records={
            result.result_ref: SimpleNamespace(contract=contract),
        },
    )
    binding = SimpleNamespace(
        supported_claim_types=("formula_component_contribution",),
    )
    resolver = SimpleNamespace(
        resolve_capability_binding=lambda ref: binding,
    )
    evidence_ref = "driver_decomposition:case-b"
    evidence = {
        "evidence_ref": evidence_ref,
        "capability_id": "driver_decomposition",
        "binding_manifest_ref": "binding:driver",
        "typed_payload": {
            "target_window_id": "target_day",
            "baseline_window_id": "previous_day",
        },
    }

    with patch(
        "bi_agent.runtime.answer_package.validate_authoritative_query_chain",
        return_value=chain,
    ):
        facts = _claim_authority_facts(
            _core_contribution_claim(),
            evidence_by_ref={evidence_ref: evidence},
            evidence_resolver=resolver,
            rows_loader=object(),
            runtime_registry=object(),
            release_resolver=None,
        )
        projected = _project_claim_from_authority(
            _core_contribution_claim(),
            facts,
        )

    assert projected["baseline"]["window_id"] == "previous_day"
    assert {
        selector["source_facts"]["paid_amount"]["baseline"]["window_id"]
        for selector in projected["fact_selectors"].values()
    } == {"previous_day"}


def test_default_driver_claim_uses_three_core_factors_and_states_neutral_success_boundary():
    state = _core_driver_state()

    claim = _default_claim_from_primary_evidence(state)
    finding = _key_findings_sentence(state)

    assert "主要贡献项是付费频次" in claim["text"]
    assert "付费人数" in claim["text"]
    assert "付费频次" in claim["text"]
    assert "单笔付费金额" in claim["text"]
    assert "支付成功率缺少独立观测" in claim["text"]
    assert "按不变处理" in claim["text"]
    assert "100%" not in claim["text"]
    assert set(claim["numbers"]) >= {
        "paid_users_contribution_share",
        "paid_frequency_contribution_share",
        "avg_order_amount_contribution_share",
    }
    assert "付费频次" in finding


def test_answer_prompts_keep_neutral_payment_success_distinct_from_observation():
    payloads = {
        "answer_synthesis": {"businessContext": {}},
        "answer_repair": {
            "answerText": "",
            "businessContext": {},
            "displayReview": {},
        },
        "final_business_summary": {
            "draftAnswer": "",
            "businessContext": {},
            "displayReview": {},
        },
    }
    for task, payload in payloads.items():
        text = "\n".join(
            message["content"]
            for message in build_prompt(task, payload).messages
        )

        assert "payment_success_assumption" not in text
        assert "observed=false" not in text
        assert "observed 100%" in text.lower()
        assert any(
            marker in text.lower()
            for marker in ("proven zero impact", "having no impact")
        )


def test_final_summary_requires_all_three_core_contribution_shares():
    state = _core_driver_state()
    claim = _default_claim_from_primary_evidence(state)
    state["draft_claims"] = [claim]
    incomplete = (
        "我对问题的理解是：确认目标日付费金额变化。\n"
        "分析脉络：先验证方向，再拆解核心因素。\n"
        "关键发现：付费频次是主要贡献项。\n"
        "最终结论：核心因素拆解已经完成。\n"
        "需要注意：支付成功率缺少独立观测。"
    )
    numbers = claim["numbers"]
    complete = incomplete.replace(
        "核心因素拆解已经完成。",
        (
            f"付费人数贡献{numbers['paid_users_contribution_share'] * 100:.1f}%，"
            f"付费频次贡献{numbers['paid_frequency_contribution_share'] * 100:.1f}%，"
            "单笔付费金额贡献"
            f"{numbers['avg_order_amount_contribution_share'] * 100:.1f}%。"
        ),
    )

    assert _final_summary_needs_display_repair(incomplete, state) is True
    assert _final_summary_needs_display_repair(complete, state) is False


def test_claim_readiness_accepts_only_declared_optional_slot_degradation():
    claim_ready = SimpleNamespace(
        status="degraded",
        input_completeness_statuses=("complete",),
        query_contract_refs=("query:components",),
        plan_payload={
            "required_input_slots": (
                {
                    "slot_id": "component_driver_scan",
                    "query_contract_refs": ("query:components",),
                },
            ),
            "optional_input_slots": (
                {
                    "slot_id": "payment_success_scan",
                    "query_contract_refs": ("query:success",),
                },
            ),
            "degradation_policy": {
                "missing_optional_input": "omit_optional_component",
            },
            "minimum_readiness": {"required_slots": "all"},
        },
        binding_payload={
            "reasons": ("missing_optional_slot:payment_success_scan",),
        },
    )
    undeclared_gap = SimpleNamespace(
        **{
            **claim_ready.__dict__,
            "binding_payload": {
                "reasons": ("missing_optional_slot:unknown_slot",),
            },
        }
    )

    claim_ready_check = getattr(
        capability_execution_module,
        "capability_binding_claim_ready",
        None,
    )
    assert claim_ready_check is not None
    assert claim_ready_check(claim_ready) is True
    assert claim_ready_check(undeclared_gap) is False


def test_answer_package_authority_gate_does_not_reject_optional_only_degraded_input():
    evidence = {
        "capability_id": "driver_decomposition",
        "analysis_contract_ref": "analysis:case-b",
        "capability_contract_ref": "runtime#driver_decomposition",
        "query_contract_refs": ("query:component-driver",),
        "result_refs": ("result:component-driver",),
        "query_execution_record_refs": ("query-record:component-driver",),
        "query_execution_record_digests": ("query-record-digest",),
        "rows_metadata_record_refs": ("rows-record:component-driver",),
        "rows_metadata_record_digests": ("rows-record-digest",),
        "completeness_report_refs": ("completeness:component-driver",),
        "completeness_record_refs": ("completeness-record:component-driver",),
        "completeness_record_digests": ("completeness-record-digest",),
        "source_snapshot_refs": ("snapshot:paid-order-success",),
        "supported_evidence_types": ("accounting_contribution",),
        "supported_claim_types": ("formula_component_contribution",),
        "maximum_claim_strength": "quantified_contribution",
        "maximum_claim_strength_rank": 3,
        "claim_strength_taxonomy_version": "1",
        "input_status": "degraded",
        "input_completeness_statuses": ("complete",),
        "binding_manifest_ref": "capability-binding:driver-decomposition",
        "binding_manifest_digest": "binding-digest",
        "limitations": ("missing_optional_slot:payment_success_scan",),
    }
    claim = {
        "claim_type": "formula_component_contribution",
        "claim_strength": "quantified_contribution",
    }

    # The resolved binding gate owns checking that the degradation reason is a
    # declared optional slot. Once it accepts the binding, the Answer Package
    # gate must not reject the same evidence merely because its status is
    # `degraded`.
    with patch(
        "bi_agent.runtime.answer_package._authority_record_errors",
        return_value=(),
    ):
        errors = _claim_authority_errors(
            claim,
            (evidence,),
            resolver=object(),
            rows_loader=object(),
            registry=object(),
            release_resolver=None,
        )

    assert errors == ()


def _core_contribution_authority_facts() -> dict:
    values = {
        "baseline": {
            "paid_amount": "100",
            "paid_users": "10",
            "paid_frequency": "2",
            "avg_order_amount": "5",
        },
        "target": {
            "paid_amount": "180",
            "paid_users": "12",
            "paid_frequency": "2.5",
            "avg_order_amount": "6",
        },
    }
    facts = tuple(
        AuthorityFact.create(
            query_contract_ref="query:component-driver",
            result_ref="result:component-driver",
            metric_id=metric_id,
            value=Decimal(value),
            window_id=("previous_day" if role == "baseline" else "target_day"),
            window_role=role,
            observation_key=(
                "2026-05-31" if role == "baseline" else "2026-06-01"
            ),
            dimensions=(),
            grain=("window_id", "observation_key"),
            value_semantics="raw_scalar",
            display_format="number",
        )
        for role, metrics in values.items()
        for metric_id, value in metrics.items()
    )
    return {
        "metric_ids": tuple(values["target"]),
        "authority_facts": facts,
        "authority_context_facts": (),
        "grains": (("window_id", "observation_key"),),
        "target_windows": ({
            "window_id": "target_day",
            "role": "target",
            "label": "2026-06-01",
            "start_inclusive": "2026-06-01",
            "end_exclusive": "2026-06-02",
            "timezone": "Africa/Lagos",
        },),
        "baseline_windows": ({
            "window_id": "previous_day",
            "role": "baseline",
            "label": "2026-05-31",
            "start_inclusive": "2026-05-31",
            "end_exclusive": "2026-06-01",
            "timezone": "Africa/Lagos",
        },),
    }


def _core_contribution_claim() -> dict:
    return {
        "text": "付费频次是核心三因素中的主要贡献项。",
        "claim_strength": "quantified_contribution",
        "claim_type": "formula_component_contribution",
        "evidence_refs": ("driver_decomposition:case-b",),
        "numbers": {
            "paid_users_contribution_share": "0.3104166666666667",
            "paid_frequency_contribution_share": "0.3791666666666667",
            "avg_order_amount_contribution_share": "0.3104166666666667",
        },
    }


def test_canonical_contribution_ratios_keep_declared_units_and_are_idempotent():
    evidence_by_ref = {
        "driver_decomposition:case-b": {
            "numeric_facts": {
                "avg_order_amount_contribution_share": 1.2622775809926496,
                "paid_frequency_contribution_share": -1.125,
            },
            "typed_payload": {
                "avg_order_amount_contribution_share": 1.2622775809926496,
                "paid_frequency_contribution_share": -1.125,
            }
        }
    }
    supplied = {
        "avg_order_amount_contribution_share": 1.2622775809926496,
        "paid_frequency_contribution_share": -1.125,
    }

    normalized = _normalize_claim_numbers(
        supplied,
        ("driver_decomposition:case-b",),
        evidence_by_ref,
    )
    normalized_again = _normalize_claim_numbers(
        normalized,
        ("driver_decomposition:case-b",),
        evidence_by_ref,
    )

    assert normalized == supplied
    assert normalized_again == supplied


def test_claim_number_normalization_drops_noncanonical_business_label_keys():
    evidence_by_ref = {
        "driver_decomposition:case-b": {
            "numeric_facts": {
                "avg_order_amount_contribution_share": 1.2622775809926496,
            },
            "typed_payload": {
                "avg_order_amount_contribution_share": 1.2622775809926496,
            }
        }
    }

    normalized = _normalize_claim_numbers(
        {"单笔付费金额贡献占比": 126.22775809926496},
        ("driver_decomposition:case-b",),
        evidence_by_ref,
    )

    assert normalized == {}


def test_claim_number_normalization_does_not_promote_diagnostic_payload_numbers():
    evidence_by_ref = {
        "driver_decomposition:case-b": {
            "numeric_facts": {
                "formula_contribution_total": 80.0,
            },
            "typed_payload": {
                "formula_contribution_total": 80.0,
                "formula_reconciliation_residual": 0.0,
            },
        }
    }

    normalized = _normalize_claim_numbers(
        {
            "formula_contribution_total": 80.0,
            "formula_reconciliation_residual": 0.0,
        },
        ("driver_decomposition:case-b",),
        evidence_by_ref,
    )

    assert normalized == {"formula_contribution_total": 80.0}


def test_core_contribution_shares_project_from_target_and_baseline_authority_facts():
    facts = _core_contribution_authority_facts()

    projected = _project_claim_from_authority(
        _core_contribution_claim(),
        facts,
    )

    assert float(projected["numbers"]["paid_users_contribution_share"]) == (
        pytest.approx(0.3104166666666667)
    )
    assert float(projected["numbers"]["paid_frequency_contribution_share"]) == (
        pytest.approx(0.3791666666666667)
    )
    assert float(projected["numbers"]["avg_order_amount_contribution_share"]) == (
        pytest.approx(0.3104166666666667)
    )
    assert set(projected["fact_refs"]) == {
        fact.fact_ref for fact in facts["authority_facts"]
    }


def test_complete_core_contribution_claim_projects_absolute_shares_and_total():
    facts = _core_contribution_authority_facts()
    claim = _core_contribution_claim()
    claim["numbers"] = {
        "paid_users_contribution": "24.8333333333333333",
        "paid_users_contribution_share": "0.3104166666666667",
        "paid_frequency_contribution": "30.3333333333333333",
        "paid_frequency_contribution_share": "0.3791666666666667",
        "avg_order_amount_contribution": "24.8333333333333333",
        "avg_order_amount_contribution_share": "0.3104166666666667",
        "formula_contribution_total": "80",
    }

    projected = _project_claim_from_authority(claim, facts)

    assert float(projected["numbers"]["paid_users_contribution"]) == pytest.approx(
        24.8333333333333333
    )
    assert float(projected["numbers"]["paid_frequency_contribution"]) == pytest.approx(
        30.3333333333333333
    )
    assert float(projected["numbers"]["avg_order_amount_contribution"]) == pytest.approx(
        24.8333333333333333
    )
    assert float(projected["numbers"]["formula_contribution_total"]) == pytest.approx(80)
    assert set(projected["numbers"]) == set(claim["numbers"])
    assert "付费人数对付费金额变化的核算贡献为" in projected["text"]
    assert "付费频次对付费金额变化的核算贡献为" in projected["text"]
    assert "单笔付费金额对付费金额变化的核算贡献为" in projected["text"]
    assert "三因素核算贡献合计为" in projected["text"]


def test_zero_net_change_keeps_absolute_contributions_without_undefined_shares():
    values = {
        "baseline": {
            "paid_amount": "100",
            "paid_users": "10",
            "paid_frequency": "2",
            "avg_order_amount": "5",
        },
        "target": {
            "paid_amount": "100",
            "paid_users": "20",
            "paid_frequency": "1",
            "avg_order_amount": "5",
        },
    }
    facts = {
        **_core_contribution_authority_facts(),
        "authority_facts": tuple(
            AuthorityFact.create(
                query_contract_ref="query:component-driver",
                result_ref="result:component-driver",
                metric_id=metric_id,
                value=Decimal(value),
                window_id=("previous_day" if role == "baseline" else "target_day"),
                window_role=role,
                observation_key=(
                    "2026-05-31" if role == "baseline" else "2026-06-01"
                ),
                dimensions=(),
                grain=("window_id", "observation_key"),
                value_semantics="raw_scalar",
                display_format="number",
            )
            for role, metrics in values.items()
            for metric_id, value in metrics.items()
        ),
    }
    claim = _core_contribution_claim()
    claim["numbers"] = {
        "paid_users_contribution": "75",
        "paid_frequency_contribution": "-75",
        "avg_order_amount_contribution": "0",
        "formula_contribution_total": "0",
    }

    projected = _project_claim_from_authority(claim, facts)

    assert set(projected["numbers"]) == set(claim["numbers"])
    assert float(projected["numbers"]["avg_order_amount_contribution"]) == 0
    assert float(projected["numbers"]["formula_contribution_total"]) == 0
    assert float(projected["numbers"]["paid_frequency_contribution"]) == -75
    assert float(projected["numbers"]["paid_users_contribution"]) == 75


def test_core_contribution_share_projection_rejects_forged_value():
    claim = _core_contribution_claim()
    claim["numbers"] = {
        **claim["numbers"],
        "paid_frequency_contribution_share": "0.9",
    }

    with pytest.raises(
        ValueError,
        match=(
            "claim_derived_value_mismatch:"
            "paid_frequency_contribution_share"
        ),
    ):
        _project_claim_from_authority(
            claim,
            _core_contribution_authority_facts(),
        )


def test_claim_readiness_accepts_bound_manifest_shape_before_persistence():
    plan = {
        "required_input_slots": (
            {
                "slot_id": "component_driver_scan",
                "query_contract_refs": ("query:components",),
            },
        ),
        "optional_input_slots": (
            {
                "slot_id": "payment_success_scan",
                "query_contract_refs": ("query:success",),
            },
        ),
        "degradation_policy": {
            "missing_optional_input": "omit_optional_component",
        },
        "minimum_readiness": {"required_slots": "all"},
    }
    binding_payload = {
        "reasons": ("missing_optional_slot:payment_success_scan",),
    }
    bound = SimpleNamespace(
        status="degraded",
        input_completeness_statuses=("complete",),
        query_contract_refs=("query:components",),
        validation_query_contract_refs=(),
        binding_manifest={"plan": plan, "binding": binding_payload},
    )

    assert capability_execution_module.capability_binding_claim_ready(bound) is True


def test_hard_verify_writes_only_authority_verified_claims_back_to_state():
    verified_claim = {
        "text": "目标日付费金额较前一天上涨。",
        "claim_type": "comparative_change",
    }
    state = {
        "request": {},
        "verifier": {},
        "draft_claims": [
            verified_claim,
            {
                "text": "单笔付费金额是主要贡献项。",
                "claim_type": "formula_component_contribution",
            },
        ],
    }
    package = {
        "admin_audit": {
            "verifier": {
                "status": "degraded",
                "errors": [{"code": "missing_required_claim"}],
                "global_errors": [],
                "accepted_claim_indexes": [0],
            },
            "verified_claims": [verified_claim],
        }
    }

    with patch(
        "bi_agent.runtime.langgraph_workflow._build_answer_package_from_state",
        return_value=package,
    ):
        _hard_verify_answer(state)

    assert state["authority_verified_claims"] == [verified_claim]
    assert _verified_claims(state) == [verified_claim]


def test_final_summary_receives_verified_subset_and_business_gap_only():
    verified_claim = {
        "text": "目标日付费金额较前一天上涨。",
        "claim_type": "comparative_change",
        "claim_strength": "observed",
        "evidence_refs": ["compare:ready"],
        "numbers": {},
        "scope": "full_sample",
        "time_window": "2026-06-01",
    }
    rejected_claim = {
        "text": "单笔付费金额是主要贡献项。",
        "claim_type": "formula_component_contribution",
        "claim_strength": "quantified_contribution",
        "evidence_refs": ["driver:rejected"],
        "numbers": {},
        "scope": "full_sample",
        "time_window": "2026-06-01",
    }
    state = {
        "request": {},
        "intent": {
            "scope": "full_sample",
            "time_window": "2026-06-01",
            "target_metric": "paid_amount",
            "target": {"label": "2026-06-01"},
            "baseline": {"label": "2026-05-31"},
            "required_claim_intents": [
                "comparative_change",
                "formula_component_contribution",
            ],
        },
        "draft_claims": [verified_claim, rejected_claim],
        "authority_verified_claims": [verified_claim],
        "verifier": {
            "status": "degraded",
            "errors": [{"code": "missing_required_claim"}],
            "global_errors": [],
            "required_claim_gaps": [
                {
                    "code": "missing_required_claim",
                    "claim_type": "formula_component_contribution",
                }
            ],
            "accepted_claim_indexes": [0],
        },
        "analysis_route": {},
        "evidence": [
            {
                "evidence_ref": "compare:ready",
                "claim_type": "comparative_change",
                "claim_input_ready": True,
                "established": True,
                "strength": "high",
                "wording_limit": "quantified",
                "limitations": [],
            }
        ],
        "checkpoint_events": [],
    }

    with patch(
        "bi_agent.runtime.langgraph_workflow._refresh_contract_gap_diagnostics",
        return_value=[],
    ):
        payload = _final_business_summary_payload(state)

    assert set(payload) == {"draftAnswer", "businessContext", "displayReview"}
    evidence = payload["businessContext"]["evidence"]
    assert [slot["statement"] for slot in evidence["claimSlots"]] == [
        verified_claim["text"]
    ]
    assert evidence["unavailableConclusions"] == [
        {
            "conclusion": "公式组成指标对目标指标变化的量化贡献",
            "state": "当前证据不足",
        }
    ]
    assert rejected_claim["text"] not in str(payload)


def test_empty_authority_verified_subset_does_not_fall_back_to_draft_claims():
    state = {
        "authority_verified_claims": [],
        "draft_claims": [{"text": "未通过验证的结论"}],
        "verifier": {"status": "degraded", "errors": []},
    }

    assert _verified_claims(state) == []


def test_degraded_verifier_with_surviving_claim_continues_after_one_repair():
    state = {
        "verifier": {
            "status": "degraded",
            "errors": [{"code": "missing_required_claim"}],
            "global_errors": [],
            "accepted_claim_indexes": [0],
        },
        "verifier_repair_attempts": 1,
    }

    assert _route_after_hard_verify(state) == "passed"
    assert _local_final_answer_hard_blockers(state) == []


def test_degraded_verifier_global_error_still_blocks_delivery():
    state = {
        "verifier": {
            "status": "degraded",
            "errors": [{"code": "authority_snapshot_mismatch"}],
            "global_errors": [{"code": "authority_snapshot_mismatch"}],
            "accepted_claim_indexes": [0],
        },
        "verifier_repair_attempts": 1,
    }

    assert _route_after_hard_verify(state) == "degrade"
    assert _local_final_answer_hard_blockers(state) == [
        "verifier_evidence_contradiction"
    ]


def test_semantic_info_issue_does_not_consume_a_repair_attempt():
    state = {
        "semantic_audit": {
            "audit_status": "passed",
            "issues": [
                {
                    "severity": "info",
                    "message": "可以补充说明基线范围。",
                }
            ],
        },
        "semantic_repair_attempts": 0,
    }

    assert _route_after_semantic_audit(state) == "verify"


@pytest.mark.parametrize("severity", ("error", "critical", "blocking"))
def test_blocking_semantic_issue_requires_repair_even_if_status_says_passed(
    severity,
):
    state = {
        "semantic_audit": {
            "audit_status": "passed",
            "issues": [{"severity": severity, "message": "结论会误导业务判断。"}],
        },
        "semantic_repair_attempts": 0,
    }

    assert _route_after_semantic_audit(state) == "repair"


def test_semantic_and_verifier_repairs_have_independent_budgets():
    state = {
        "verifier": {
            "status": "failed",
            "errors": [{"code": "number_mismatch"}],
            "global_errors": [],
            "accepted_claim_indexes": [],
        },
        "semantic_repair_attempts": 1,
        "verifier_repair_attempts": 0,
        "answer_repair_attempts": 1,
    }

    assert _route_after_hard_verify(state) == "repair"


def test_answer_repair_charges_the_failure_specific_budget():
    state = _state("compare_periods")
    state.update({
        "request": {
            **state["request"],
            "run_mode": "production",
            "question": "目标日付费金额相比前一天发生了什么变化？",
        },
        "retry_context": {"failure_type": "semantic_audit"},
        "answer_repair_attempts": 0,
        "semantic_repair_attempts": 0,
        "verifier_repair_attempts": 0,
        "answer_text": "待修复答案",
        "draft_claims": [],
        "semantic_audit": {},
        "verifier": {},
        "evidence_brief": {},
    })
    repaired_output = {"answer_text": "已修复答案"}

    with (
        patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            return_value=repaired_output,
        ),
        patch(
            "bi_agent.runtime.langgraph_workflow._ensure_business_narrative_answer"
        ),
        patch(
            "bi_agent.runtime.langgraph_workflow._answer_synthesis_context",
            return_value={},
        ),
    ):
        _repair_answer(state)

    assert state["answer_repair_attempts"] == 1
    assert state["semantic_repair_attempts"] == 1
    assert state["verifier_repair_attempts"] == 0

    state["retry_context"] = {"failure_type": "verifier"}
    with (
        patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            return_value=repaired_output,
        ),
        patch(
            "bi_agent.runtime.langgraph_workflow._ensure_business_narrative_answer"
        ),
        patch(
            "bi_agent.runtime.langgraph_workflow._answer_synthesis_context",
            return_value={},
        ),
    ):
        _repair_answer(state)

    assert state["answer_repair_attempts"] == 2
    assert state["semantic_repair_attempts"] == 1
    assert state["verifier_repair_attempts"] == 1


def test_partial_delivery_uses_business_labels_for_verified_comparison():
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    claim = {
        "text": (
            "目标期（target_day）相较基线（previous_day）"
            "paid_amount增加4097679。"
        ),
        "claim_type": "comparative_change",
        "target_metric": "paid_amount",
        "target": {"label": "2026-06-01"},
        "baseline": {"label": "2026-05-31"},
        "comparison_direction": "positive",
        "numbers": {
            "target_value": 308240309,
            "baseline_value": 304142630,
            "absolute_change": 4097679,
            "relative_change": 0.01347288606,
        },
    }

    text = _partial_claim_delivery_text(
        (claim,),
        {
            "required_claim_gaps": [
                {"claim_type": "formula_component_contribution"}
            ]
        },
        runtime_registry=registry,
    )

    assert "2026-06-01 的付费金额" in text
    assert "2026-05-31" in text
    assert "target_day" not in text
    assert "previous_day" not in text
    assert "paid_amount" not in text
    assert "因素贡献结论本轮未发布" in text


def test_persisted_authority_reprojects_degraded_verified_subset():
    factual_claim = {
        "text": "目标日付费金额较前一天上涨20。",
        "claim_type": "comparative_change",
        "claim_strength": "observed",
        "evidence_refs": ["compare:ready"],
        "numbers": {"absolute_change": 20.0},
    }
    persisted_claim = {
        **factual_claim,
        "claim_ref": "claim:verified:1",
        "claim_id": "claim-id-1",
        "claim_digest": "claim-digest-1",
        "context_manifest_ref": "context:1",
        "artifact_refs": ["artifact:1"],
        "memory_refs": [],
        "reuse_decisions": [],
        "provenance_record_ref": "provenance:1",
    }
    verifier = {
        "status": "degraded",
        "errors": [{"code": "missing_required_claim"}],
        "global_errors": [],
        "claim_rejections": [],
        "required_claim_gaps": [{"code": "missing_required_claim"}],
        "accepted_claim_indexes": [0],
        "rejected_claim_indexes": [],
    }
    package = {
        "status": "draft",
        "sections": [
            {
                "section_id": "summary",
                "payload": {
                    "claims": [factual_claim],
                    "claim_groups": [],
                    "visualization_plan": {"blocks": []},
                },
            },
            {"section_id": "admin_audit", "payload": {}},
        ],
        "admin_audit": {"verifier": verifier},
    }
    persistence_records = {
        "verified_claims": [persisted_claim],
        "trusted_provenance_records": [],
        "context_manifests": [{"manifest_id": "context:1"}],
        "analysis_contract": {"analysis_contract_id": "analysis:1"},
    }

    projected = reproject_answer_package_from_persisted_authority(
        package,
        persistence_records=persistence_records,
    )

    assert projected["admin_audit"]["verifier"]["status"] == "degraded"
    assert (
        projected["admin_audit"]["analysis_runtime_persistence"]["status"]
        == "bundle_validated"
    )
    assert projected["verified_claims"] == [persisted_claim]
    assert projected["sections"][0]["payload"]["claims"][0]["claim_id"].startswith(
        "claim:sha256:"
    )
