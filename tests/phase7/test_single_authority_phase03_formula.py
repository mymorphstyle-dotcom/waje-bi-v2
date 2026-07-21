from __future__ import annotations

from pathlib import Path

import pytest

from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.formula_graph import (
    FormulaContractError,
    decompose_formula_change,
    evaluate_formula,
    formula_metric_ids,
    load_formula_graph,
    reconcile_hierarchy_sum,
    validate_formula_ast,
)


METRIC_CONTRACT = Path("contracts/metrics/paid-amount.metric.yaml")


def test_paid_amount_contract_has_typed_source_ast_for_every_path() -> None:
    contract = load_contract(METRIC_CONTRACT)
    declared_path_ids = {path["path_id"] for path in contract["decomposition_paths"]}

    graph = load_formula_graph(METRIC_CONTRACT)

    assert {path.path_id for path in graph.paths} == declared_path_ids
    assert len(graph.paths) == 21
    assert all(path.source_ast.kind == "relationship" for path in graph.paths)


def test_frequency_ticket_size_keeps_distinct_source_and_runtime_ast() -> None:
    graph = load_formula_graph(METRIC_CONTRACT)
    path = graph.path("frequency_ticket_size")

    assert formula_metric_ids(path.source_ast) == (
        "avg_paid_amount_per_payment",
        "paid_amount",
        "paid_dau",
        "paid_user_conversion_rate",
        "payment_frequency_per_paid_user",
    )
    assert formula_metric_ids(path.runtime_ast) == (
        "avg_order_amount",
        "paid_amount",
        "paid_frequency",
        "paid_users",
    )
    assert "paid_dau" not in formula_metric_ids(path.runtime_ast)
    assert path.runtime_ast.kind == "relationship"
    assert (
        evaluate_formula(
            path.runtime_ast,
            metrics={
                "paid_users": 100,
                "paid_frequency": 2,
                "avg_order_amount": 2.2,
            },
        ).value
        == 440
    )


def test_ast_validator_accepts_declared_operators_and_rejects_text_formulas() -> None:
    ast = validate_formula_ast(
        {
            "op": "add",
            "args": [
                {"op": "metric", "metric_id": "a"},
                {
                    "op": "divide",
                    "left": {"op": "metric", "metric_id": "b"},
                    "right": {"op": "const", "value": 2},
                },
            ],
        }
    )

    assert evaluate_formula(ast, metrics={"a": 3, "b": 8}).value == 7
    with pytest.raises(FormulaContractError, match="formula_ast_must_be_mapping"):
        validate_formula_ast("a + b")
    with pytest.raises(FormulaContractError, match="formula_ast_operator_invalid"):
        validate_formula_ast({"op": "python", "source": "a + b"})


def test_projection_uses_only_declared_metric_and_dimension_bindings() -> None:
    ast = validate_formula_ast(
        {
            "op": "projection",
            "expression": {
                "op": "sum_by",
                "dimension_id": "source_region",
                "expression": {"op": "metric", "metric_id": "source_amount"},
            },
            "metric_bindings": {"source_amount": "runtime_amount"},
            "dimension_bindings": {"source_region": "runtime_region"},
        }
    )

    result = evaluate_formula(
        ast,
        metrics={},
        hierarchies={
            "runtime_region": (
                {"runtime_amount": 10},
                {"runtime_amount": 15},
            )
        },
    )

    assert result.status == "evaluated"
    assert result.value == 25


def test_missing_factor_stays_missing_without_neutral_value_fallback() -> None:
    ast = validate_formula_ast(
        {
            "op": "multiply",
            "args": [
                {"op": "metric", "metric_id": "paid_users"},
                {"op": "metric", "metric_id": "paid_frequency"},
                {"op": "metric", "metric_id": "avg_order_amount"},
            ],
        }
    )

    evaluated = evaluate_formula(
        ast,
        metrics={"paid_users": 100, "avg_order_amount": 2},
    )
    decomposition = decompose_formula_change(
        ast,
        baseline_metrics={"paid_users": 100, "avg_order_amount": 2},
        target_metrics={"paid_users": 110, "avg_order_amount": 2},
        factor_metric_ids=(
            "paid_users",
            "paid_frequency",
            "avg_order_amount",
        ),
        observed_baseline=400,
        observed_target=440,
    )

    assert evaluated.status == "missing"
    assert evaluated.value is None
    assert evaluated.missing_metric_ids == ("paid_frequency",)
    assert decomposition.status == "missing"
    assert decomposition.contributions == ()
    assert decomposition.missing_metric_ids == ("paid_frequency",)


def test_n_factor_shapley_is_order_independent_and_exactly_reconciled() -> None:
    ast = validate_formula_ast(
        {
            "op": "multiply",
            "args": [
                {"op": "metric", "metric_id": "active"},
                {"op": "metric", "metric_id": "conversion"},
                {"op": "metric", "metric_id": "frequency"},
                {"op": "metric", "metric_id": "ticket"},
            ],
        }
    )
    baseline = {"active": 100, "conversion": 0.2, "frequency": 2, "ticket": 5}
    target = {"active": 120, "conversion": 0.25, "frequency": 1.8, "ticket": 6}

    first = decompose_formula_change(
        ast,
        baseline_metrics=baseline,
        target_metrics=target,
        factor_metric_ids=("active", "conversion", "frequency", "ticket"),
        observed_baseline=200,
        observed_target=324,
    )
    second = decompose_formula_change(
        ast,
        baseline_metrics=baseline,
        target_metrics=target,
        factor_metric_ids=("ticket", "frequency", "conversion", "active"),
        observed_baseline=200,
        observed_target=324,
    )

    assert first.status == "reconciled"
    assert first.direction == "increase"
    assert first.component_residual == 0
    assert first.movement_residual == 0
    assert first.contribution_total == pytest.approx(124)
    assert first.contributions == second.contributions


def test_observed_target_baseline_direction_must_match_formula_direction() -> None:
    ast = validate_formula_ast(
        {
            "op": "multiply",
            "args": [
                {"op": "metric", "metric_id": "users"},
                {"op": "metric", "metric_id": "value"},
            ],
        }
    )

    result = decompose_formula_change(
        ast,
        baseline_metrics={"users": 10, "value": 10},
        target_metrics={"users": 11, "value": 10},
        factor_metric_ids=("users", "value"),
        observed_baseline=100,
        observed_target=90,
    )

    assert result.status == "mismatch"
    assert result.direction == "increase"
    assert result.observed_direction == "decrease"
    assert result.movement_residual == -20


def test_hierarchy_sum_reconciles_and_preserves_missing_members() -> None:
    ast = validate_formula_ast(
        {
            "op": "sum_by",
            "dimension_id": "region",
            "expression": {"op": "metric", "metric_id": "paid_amount"},
        }
    )

    reconciled = reconcile_hierarchy_sum(
        ast,
        target_value=30,
        hierarchies={
            "region": (
                {"paid_amount": 10},
                {"paid_amount": 20},
            )
        },
    )
    missing = reconcile_hierarchy_sum(
        ast,
        target_value=30,
        hierarchies={
            "region": (
                {"paid_amount": 10},
                {"region": "unknown"},
            )
        },
    )

    assert reconciled.status == "reconciled"
    assert reconciled.residual == 0
    assert missing.status == "missing"
    assert missing.actual_value is None
    assert missing.missing_metric_ids == ("paid_amount",)
