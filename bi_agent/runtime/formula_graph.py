from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
import math
from pathlib import Path
from typing import Any, Literal, TypeAlias

from bi_agent.runtime.contracts import load_contract


class FormulaContractError(ValueError):
    pass


@dataclass(frozen=True)
class MetricNode:
    metric_id: str

    @property
    def kind(self) -> str:
        return "metric"


@dataclass(frozen=True)
class DimensionNode:
    dimension_id: str

    @property
    def kind(self) -> str:
        return "dimension"


@dataclass(frozen=True)
class ConstNode:
    value: Fraction

    @property
    def kind(self) -> str:
        return "const"


@dataclass(frozen=True)
class AddNode:
    args: tuple[FormulaNode, ...]

    @property
    def kind(self) -> str:
        return "add"


@dataclass(frozen=True)
class SubtractNode:
    left: FormulaNode
    right: FormulaNode

    @property
    def kind(self) -> str:
        return "subtract"


@dataclass(frozen=True)
class MultiplyNode:
    args: tuple[FormulaNode, ...]

    @property
    def kind(self) -> str:
        return "multiply"


@dataclass(frozen=True)
class DivideNode:
    left: FormulaNode
    right: FormulaNode

    @property
    def kind(self) -> str:
        return "divide"


@dataclass(frozen=True)
class SumByNode:
    dimension_id: str
    expression: FormulaNode

    @property
    def kind(self) -> str:
        return "sum_by"


@dataclass(frozen=True)
class RelationshipNode:
    relation: Literal[
        "equals",
        "approximately_equals",
        "collection",
        "may_vary_with",
    ]
    left: FormulaNode | None = None
    right: FormulaNode | None = None
    inputs: tuple[FormulaNode, ...] = ()

    @property
    def kind(self) -> str:
        return "relationship"


@dataclass(frozen=True)
class ProjectionNode:
    expression: FormulaNode
    metric_bindings: tuple[tuple[str, str], ...]
    dimension_bindings: tuple[tuple[str, str], ...]

    @property
    def kind(self) -> str:
        return "projection"


FormulaNode: TypeAlias = (
    MetricNode
    | DimensionNode
    | ConstNode
    | AddNode
    | SubtractNode
    | MultiplyNode
    | DivideNode
    | SumByNode
    | RelationshipNode
    | ProjectionNode
)


@dataclass(frozen=True)
class FormulaPath:
    path_id: str
    source_ast: FormulaNode
    runtime_ast: FormulaNode
    reconciliation_required: bool
    residual_policy: str
    absolute_tolerance: float
    relative_tolerance: float


@dataclass(frozen=True)
class FormulaGraph:
    metric_id: str
    ast_contract_version: str
    paths: tuple[FormulaPath, ...]

    def path(self, path_id: str) -> FormulaPath:
        for path in self.paths:
            if path.path_id == path_id:
                return path
        raise FormulaContractError(f"formula_path_unknown:{path_id}")


@dataclass(frozen=True)
class FormulaEvaluation:
    status: Literal["evaluated", "missing", "invalid", "non_evaluable"]
    value: float | None = None
    missing_metric_ids: tuple[str, ...] = ()
    missing_dimension_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class FormulaContribution:
    metric_id: str
    baseline_value: float
    target_value: float
    delta: float
    contribution: float
    contribution_share: float | None


@dataclass(frozen=True)
class FormulaChangeDecomposition:
    status: Literal[
        "reconciled",
        "mismatch",
        "missing",
        "invalid",
        "non_evaluable",
    ]
    direction: Literal["increase", "decrease", "flat"] | None = None
    observed_direction: Literal["increase", "decrease", "flat"] | None = None
    baseline_value: float | None = None
    target_value: float | None = None
    observed_baseline: float | None = None
    observed_target: float | None = None
    delta: float | None = None
    observed_delta: float | None = None
    contributions: tuple[FormulaContribution, ...] = ()
    contribution_total: float | None = None
    component_residual: float | None = None
    baseline_residual: float | None = None
    target_residual: float | None = None
    movement_residual: float | None = None
    tolerance: float | None = None
    missing_metric_ids: tuple[str, ...] = ()
    missing_dimension_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class HierarchyReconciliation:
    status: Literal["reconciled", "mismatch", "missing", "invalid"]
    expected_value: float | None = None
    actual_value: float | None = None
    residual: float | None = None
    tolerance: float | None = None
    missing_metric_ids: tuple[str, ...] = ()
    missing_dimension_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class _ExactEvaluation:
    status: Literal["evaluated", "missing", "invalid", "non_evaluable"]
    value: Fraction | None = None
    missing_metric_ids: frozenset[str] = frozenset()
    missing_dimension_ids: frozenset[str] = frozenset()
    reason: str | None = None


_RELATIONS = frozenset(
    {"equals", "approximately_equals", "collection", "may_vary_with"}
)


def validate_formula_ast(payload: Any) -> FormulaNode:
    if not isinstance(payload, Mapping):
        raise FormulaContractError("formula_ast_must_be_mapping")
    operator = _required_id(payload.get("op"), "formula_ast_operator_invalid")
    if operator == "metric":
        _require_fields(payload, {"op", "metric_id"})
        return MetricNode(
            _required_id(payload.get("metric_id"), "formula_metric_id_invalid")
        )
    if operator == "dimension":
        _require_fields(payload, {"op", "dimension_id"})
        return DimensionNode(
            _required_id(payload.get("dimension_id"), "formula_dimension_id_invalid")
        )
    if operator == "const":
        _require_fields(payload, {"op", "value"})
        value = _number(payload.get("value"))
        if value is None:
            raise FormulaContractError("formula_const_value_invalid")
        return ConstNode(value)
    if operator in {"add", "multiply"}:
        _require_fields(payload, {"op", "args"})
        args = _node_sequence(payload.get("args"))
        if len(args) < 2:
            raise FormulaContractError("formula_ast_args_too_short")
        if operator == "add":
            return AddNode(args)
        return MultiplyNode(args)
    if operator in {"subtract", "divide"}:
        _require_fields(payload, {"op", "left", "right"})
        left = validate_formula_ast(payload.get("left"))
        right = validate_formula_ast(payload.get("right"))
        if operator == "subtract":
            return SubtractNode(left=left, right=right)
        return DivideNode(left=left, right=right)
    if operator == "sum_by":
        _require_fields(payload, {"op", "dimension_id", "expression"})
        return SumByNode(
            dimension_id=_required_id(
                payload.get("dimension_id"), "formula_dimension_id_invalid"
            ),
            expression=validate_formula_ast(payload.get("expression")),
        )
    if operator == "relationship":
        relation = _required_id(payload.get("relation"), "formula_relationship_invalid")
        if relation not in _RELATIONS:
            raise FormulaContractError("formula_relationship_invalid")
        if relation in {"equals", "approximately_equals"}:
            _require_fields(payload, {"op", "relation", "left", "right"})
            return RelationshipNode(
                relation=relation,
                left=validate_formula_ast(payload.get("left")),
                right=validate_formula_ast(payload.get("right")),
            )
        _require_fields(payload, {"op", "relation", "inputs"})
        inputs = _node_sequence(payload.get("inputs"))
        if len(inputs) < 2:
            raise FormulaContractError("formula_relationship_inputs_too_short")
        return RelationshipNode(relation=relation, inputs=inputs)
    if operator == "projection":
        _require_fields(
            payload,
            {"op", "expression", "metric_bindings", "dimension_bindings"},
        )
        return ProjectionNode(
            expression=validate_formula_ast(payload.get("expression")),
            metric_bindings=_bindings(
                payload.get("metric_bindings"), "formula_metric_bindings_invalid"
            ),
            dimension_bindings=_bindings(
                payload.get("dimension_bindings"),
                "formula_dimension_bindings_invalid",
            ),
        )
    raise FormulaContractError("formula_ast_operator_invalid")


def load_formula_graph(contract_path: str | Path) -> FormulaGraph:
    contract = load_contract(contract_path)
    metric_id = _required_id(contract.get("metric_id"), "formula_metric_id_invalid")
    ast_contract = contract.get("formula_ast_contract")
    if not isinstance(ast_contract, Mapping):
        raise FormulaContractError("formula_ast_contract_missing")
    _require_fields(
        ast_contract,
        {"version", "absolute_tolerance", "relative_tolerance"},
    )
    version = _required_id(
        ast_contract.get("version"), "formula_ast_contract_version_invalid"
    )
    absolute_tolerance = _nonnegative_float(
        ast_contract.get("absolute_tolerance"), "formula_absolute_tolerance_invalid"
    )
    relative_tolerance = _nonnegative_float(
        ast_contract.get("relative_tolerance"), "formula_relative_tolerance_invalid"
    )
    raw_paths = contract.get("decomposition_paths")
    if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
        raise FormulaContractError("formula_decomposition_paths_invalid")
    paths: list[FormulaPath] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, Mapping):
            raise FormulaContractError("formula_decomposition_path_invalid")
        path_id = _required_id(raw_path.get("path_id"), "formula_path_id_invalid")
        if path_id in seen:
            raise FormulaContractError(f"formula_path_duplicated:{path_id}")
        seen.add(path_id)
        source_ast = validate_formula_ast(raw_path.get("source_ast"))
        projection = raw_path.get("runtime_projection") or {}
        if not isinstance(projection, Mapping):
            raise FormulaContractError(f"formula_runtime_projection_invalid:{path_id}")
        raw_runtime_ast = projection.get("runtime_ast")
        if projection.get("runtime_expression") and raw_runtime_ast is None:
            raise FormulaContractError(f"formula_runtime_ast_missing:{path_id}")
        if raw_path.get("accepted_runtime_expression") and raw_runtime_ast is None:
            raise FormulaContractError(f"formula_runtime_ast_missing:{path_id}")
        if raw_runtime_ast is not None:
            runtime_ast = validate_formula_ast(raw_runtime_ast)
        else:
            runtime_ast = ProjectionNode(
                expression=source_ast,
                metric_bindings=_bindings(
                    projection.get("component_bindings") or {},
                    "formula_metric_bindings_invalid",
                ),
                dimension_bindings=_bindings(
                    projection.get("dimension_bindings") or {},
                    "formula_dimension_bindings_invalid",
                ),
            )
        reconciliation = raw_path.get("reconciliation")
        if not isinstance(reconciliation, Mapping):
            raise FormulaContractError(
                f"formula_reconciliation_contract_invalid:{path_id}"
            )
        required = reconciliation.get("required")
        if not isinstance(required, bool):
            raise FormulaContractError(
                f"formula_reconciliation_required_invalid:{path_id}"
            )
        residual_policy = _required_id(
            reconciliation.get("residual_policy"),
            f"formula_residual_policy_invalid:{path_id}",
        )
        paths.append(
            FormulaPath(
                path_id=path_id,
                source_ast=source_ast,
                runtime_ast=runtime_ast,
                reconciliation_required=required,
                residual_policy=residual_policy,
                absolute_tolerance=absolute_tolerance,
                relative_tolerance=relative_tolerance,
            )
        )
    return FormulaGraph(
        metric_id=metric_id,
        ast_contract_version=version,
        paths=tuple(paths),
    )


def formula_metric_ids(ast: FormulaNode) -> tuple[str, ...]:
    return tuple(sorted(_metric_ids(ast, {}, {})))


def evaluate_formula(
    ast: FormulaNode | Mapping[str, Any],
    *,
    metrics: Mapping[str, Any],
    hierarchies: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> FormulaEvaluation:
    node = validate_formula_ast(ast) if isinstance(ast, Mapping) else ast
    result = _evaluate_exact(
        node,
        metrics=metrics,
        hierarchies=hierarchies or {},
        metric_bindings={},
        dimension_bindings={},
        row=None,
    )
    return FormulaEvaluation(
        status=result.status,
        value=_float(result.value),
        missing_metric_ids=tuple(sorted(result.missing_metric_ids)),
        missing_dimension_ids=tuple(sorted(result.missing_dimension_ids)),
        reason=result.reason,
    )


def decompose_formula_change(
    ast: FormulaNode | Mapping[str, Any],
    *,
    baseline_metrics: Mapping[str, Any],
    target_metrics: Mapping[str, Any],
    factor_metric_ids: Sequence[str],
    observed_baseline: Any,
    observed_target: Any,
    absolute_tolerance: float = 1e-9,
    relative_tolerance: float = 1e-9,
) -> FormulaChangeDecomposition:
    node = validate_formula_ast(ast) if isinstance(ast, Mapping) else ast
    expression = _equation_expression(node)
    if expression is None:
        return FormulaChangeDecomposition(
            status="non_evaluable", reason="formula_relationship_non_evaluable"
        )
    factor_ids = tuple(sorted(_validated_factor_ids(factor_metric_ids)))
    expression_metrics = set(formula_metric_ids(expression))
    if set(factor_ids) != expression_metrics:
        raise FormulaContractError("formula_factor_set_must_match_expression")
    baseline = _evaluate_exact(
        expression,
        metrics=baseline_metrics,
        hierarchies={},
        metric_bindings={},
        dimension_bindings={},
        row=None,
    )
    target = _evaluate_exact(
        expression,
        metrics=target_metrics,
        hierarchies={},
        metric_bindings={},
        dimension_bindings={},
        row=None,
    )
    missing_metrics = baseline.missing_metric_ids | target.missing_metric_ids
    missing_dimensions = baseline.missing_dimension_ids | target.missing_dimension_ids
    if missing_metrics or missing_dimensions:
        return FormulaChangeDecomposition(
            status="missing",
            missing_metric_ids=tuple(sorted(missing_metrics)),
            missing_dimension_ids=tuple(sorted(missing_dimensions)),
            reason="formula_input_missing",
        )
    if baseline.status != "evaluated" or target.status != "evaluated":
        return FormulaChangeDecomposition(
            status=(
                "non_evaluable"
                if "non_evaluable" in {baseline.status, target.status}
                else "invalid"
            ),
            reason=baseline.reason or target.reason,
        )
    observed_baseline_exact = _number(observed_baseline)
    observed_target_exact = _number(observed_target)
    if observed_baseline_exact is None or observed_target_exact is None:
        return FormulaChangeDecomposition(
            status="missing", reason="observed_target_or_baseline_missing"
        )
    absolute = _nonnegative_fraction(
        absolute_tolerance, "formula_absolute_tolerance_invalid"
    )
    relative = _nonnegative_fraction(
        relative_tolerance, "formula_relative_tolerance_invalid"
    )
    assert baseline.value is not None and target.value is not None
    formula_delta = target.value - baseline.value
    observed_delta = observed_target_exact - observed_baseline_exact
    exact_contributions = _shapley_contributions(
        expression=expression,
        baseline_metrics=baseline_metrics,
        target_metrics=target_metrics,
        factor_ids=factor_ids,
    )
    contribution_total = sum(exact_contributions.values(), Fraction(0))
    component_residual = formula_delta - contribution_total
    baseline_residual = observed_baseline_exact - baseline.value
    target_residual = observed_target_exact - target.value
    movement_residual = observed_delta - formula_delta
    scale = max(
        abs(observed_baseline_exact),
        abs(observed_target_exact),
        abs(baseline.value),
        abs(target.value),
        Fraction(1),
    )
    tolerance = max(absolute, relative * scale)
    direction = _direction(formula_delta)
    observed_direction = _direction(observed_delta)
    status: Literal["reconciled", "mismatch"] = (
        "reconciled"
        if direction == observed_direction
        and all(
            abs(residual) <= tolerance
            for residual in (
                component_residual,
                baseline_residual,
                target_residual,
                movement_residual,
            )
        )
        else "mismatch"
    )
    contributions = tuple(
        FormulaContribution(
            metric_id=metric_id,
            baseline_value=_float_required(_number(baseline_metrics[metric_id])),
            target_value=_float_required(_number(target_metrics[metric_id])),
            delta=_float_required(
                _number(target_metrics[metric_id])
                - _number(baseline_metrics[metric_id])  # type: ignore[operator]
            ),
            contribution=_float_required(exact_contributions[metric_id]),
            contribution_share=(
                _float(exact_contributions[metric_id] / contribution_total)
                if contribution_total
                else None
            ),
        )
        for metric_id in factor_ids
    )
    return FormulaChangeDecomposition(
        status=status,
        direction=direction,
        observed_direction=observed_direction,
        baseline_value=_float(baseline.value),
        target_value=_float(target.value),
        observed_baseline=_float(observed_baseline_exact),
        observed_target=_float(observed_target_exact),
        delta=_float(formula_delta),
        observed_delta=_float(observed_delta),
        contributions=contributions,
        contribution_total=_float(contribution_total),
        component_residual=_float(component_residual),
        baseline_residual=_float(baseline_residual),
        target_residual=_float(target_residual),
        movement_residual=_float(movement_residual),
        tolerance=_float(tolerance),
    )


def reconcile_hierarchy_sum(
    ast: FormulaNode | Mapping[str, Any],
    *,
    target_value: Any,
    hierarchies: Mapping[str, Sequence[Mapping[str, Any]]],
    metrics: Mapping[str, Any] | None = None,
    absolute_tolerance: float = 1e-9,
    relative_tolerance: float = 1e-9,
) -> HierarchyReconciliation:
    node = validate_formula_ast(ast) if isinstance(ast, Mapping) else ast
    expression = _equation_expression(node)
    if expression is None or not _contains_sum_by(expression):
        raise FormulaContractError("hierarchy_reconciliation_requires_sum_by")
    evaluated = _evaluate_exact(
        expression,
        metrics=metrics or {},
        hierarchies=hierarchies,
        metric_bindings={},
        dimension_bindings={},
        row=None,
    )
    if evaluated.status == "missing":
        return HierarchyReconciliation(
            status="missing",
            missing_metric_ids=tuple(sorted(evaluated.missing_metric_ids)),
            missing_dimension_ids=tuple(sorted(evaluated.missing_dimension_ids)),
            reason=evaluated.reason,
        )
    if evaluated.status != "evaluated" or evaluated.value is None:
        return HierarchyReconciliation(
            status="invalid", reason=evaluated.reason or "hierarchy_sum_invalid"
        )
    expected = _number(target_value)
    if expected is None:
        return HierarchyReconciliation(
            status="missing", reason="hierarchy_target_value_missing"
        )
    absolute = _nonnegative_fraction(
        absolute_tolerance, "formula_absolute_tolerance_invalid"
    )
    relative = _nonnegative_fraction(
        relative_tolerance, "formula_relative_tolerance_invalid"
    )
    tolerance = max(
        absolute,
        relative * max(abs(expected), abs(evaluated.value), Fraction(1)),
    )
    residual = expected - evaluated.value
    return HierarchyReconciliation(
        status="reconciled" if abs(residual) <= tolerance else "mismatch",
        expected_value=_float(expected),
        actual_value=_float(evaluated.value),
        residual=_float(residual),
        tolerance=_float(tolerance),
    )


def _evaluate_exact(
    node: FormulaNode,
    *,
    metrics: Mapping[str, Any],
    hierarchies: Mapping[str, Sequence[Mapping[str, Any]]],
    metric_bindings: Mapping[str, str],
    dimension_bindings: Mapping[str, str],
    row: Mapping[str, Any] | None,
) -> _ExactEvaluation:
    if isinstance(node, MetricNode):
        metric_id = metric_bindings.get(node.metric_id, node.metric_id)
        source = row if row is not None else metrics
        if metric_id not in source or source.get(metric_id) is None:
            return _ExactEvaluation(
                status="missing", missing_metric_ids=frozenset({metric_id})
            )
        value = _number(source.get(metric_id))
        if value is None:
            return _ExactEvaluation(
                status="invalid", reason=f"formula_metric_value_invalid:{metric_id}"
            )
        return _ExactEvaluation(status="evaluated", value=value)
    if isinstance(node, DimensionNode):
        return _ExactEvaluation(
            status="non_evaluable",
            reason=f"formula_dimension_reference:{node.dimension_id}",
        )
    if isinstance(node, ConstNode):
        return _ExactEvaluation(status="evaluated", value=node.value)
    if isinstance(node, (AddNode, MultiplyNode)):
        values = tuple(
            _evaluate_exact(
                child,
                metrics=metrics,
                hierarchies=hierarchies,
                metric_bindings=metric_bindings,
                dimension_bindings=dimension_bindings,
                row=row,
            )
            for child in node.args
        )
        failure = _merge_failures(values)
        if failure is not None:
            return failure
        exact_values = tuple(value.value for value in values)
        assert all(value is not None for value in exact_values)
        if isinstance(node, AddNode):
            return _ExactEvaluation(
                status="evaluated",
                value=sum(exact_values, Fraction(0)),  # type: ignore[arg-type]
            )
        product = Fraction(1)
        for value in exact_values:
            product *= value  # type: ignore[operator]
        return _ExactEvaluation(status="evaluated", value=product)
    if isinstance(node, (SubtractNode, DivideNode)):
        left = _evaluate_exact(
            node.left,
            metrics=metrics,
            hierarchies=hierarchies,
            metric_bindings=metric_bindings,
            dimension_bindings=dimension_bindings,
            row=row,
        )
        right = _evaluate_exact(
            node.right,
            metrics=metrics,
            hierarchies=hierarchies,
            metric_bindings=metric_bindings,
            dimension_bindings=dimension_bindings,
            row=row,
        )
        failure = _merge_failures((left, right))
        if failure is not None:
            return failure
        assert left.value is not None and right.value is not None
        if isinstance(node, SubtractNode):
            return _ExactEvaluation(status="evaluated", value=left.value - right.value)
        if right.value == 0:
            return _ExactEvaluation(status="invalid", reason="formula_division_by_zero")
        return _ExactEvaluation(status="evaluated", value=left.value / right.value)
    if isinstance(node, SumByNode):
        dimension_id = dimension_bindings.get(node.dimension_id, node.dimension_id)
        if dimension_id not in hierarchies:
            return _ExactEvaluation(
                status="missing",
                missing_dimension_ids=frozenset({dimension_id}),
            )
        values = tuple(
            _evaluate_exact(
                node.expression,
                metrics=metrics,
                hierarchies=hierarchies,
                metric_bindings=metric_bindings,
                dimension_bindings=dimension_bindings,
                row=member,
            )
            for member in hierarchies[dimension_id]
        )
        failure = _merge_failures(values)
        if failure is not None:
            return failure
        return _ExactEvaluation(
            status="evaluated",
            value=sum(
                (value.value for value in values),
                Fraction(0),
            ),  # type: ignore[arg-type]
        )
    if isinstance(node, RelationshipNode):
        if node.relation in {"equals", "approximately_equals"}:
            assert node.right is not None
            return _evaluate_exact(
                node.right,
                metrics=metrics,
                hierarchies=hierarchies,
                metric_bindings=metric_bindings,
                dimension_bindings=dimension_bindings,
                row=row,
            )
        return _ExactEvaluation(
            status="non_evaluable", reason=f"formula_relationship:{node.relation}"
        )
    if isinstance(node, ProjectionNode):
        projected_metrics = dict(metric_bindings)
        projected_metrics.update(node.metric_bindings)
        projected_dimensions = dict(dimension_bindings)
        projected_dimensions.update(node.dimension_bindings)
        return _evaluate_exact(
            node.expression,
            metrics=metrics,
            hierarchies=hierarchies,
            metric_bindings=projected_metrics,
            dimension_bindings=projected_dimensions,
            row=row,
        )
    raise FormulaContractError("formula_ast_node_invalid")


def _equation_expression(node: FormulaNode) -> FormulaNode | None:
    if isinstance(node, ProjectionNode):
        inner = _equation_expression(node.expression)
        if inner is None:
            return None
        return ProjectionNode(
            expression=inner,
            metric_bindings=node.metric_bindings,
            dimension_bindings=node.dimension_bindings,
        )
    if isinstance(node, RelationshipNode):
        if node.relation not in {"equals", "approximately_equals"}:
            return None
        return node.right
    return node


def _shapley_contributions(
    *,
    expression: FormulaNode,
    baseline_metrics: Mapping[str, Any],
    target_metrics: Mapping[str, Any],
    factor_ids: tuple[str, ...],
) -> dict[str, Fraction]:
    factor_count = len(factor_ids)
    factorial = math.factorial
    denominator = factorial(factor_count)
    output: dict[str, Fraction] = {}
    for metric_id in factor_ids:
        others = tuple(value for value in factor_ids if value != metric_id)
        effect = Fraction(0)
        for size in range(len(others) + 1):
            weight = Fraction(
                factorial(size) * factorial(factor_count - size - 1),
                denominator,
            )
            for subset in combinations(others, size):
                without = dict(baseline_metrics)
                for selected in subset:
                    without[selected] = target_metrics[selected]
                with_metric = dict(without)
                with_metric[metric_id] = target_metrics[metric_id]
                before = _evaluate_exact(
                    expression,
                    metrics=without,
                    hierarchies={},
                    metric_bindings={},
                    dimension_bindings={},
                    row=None,
                )
                after = _evaluate_exact(
                    expression,
                    metrics=with_metric,
                    hierarchies={},
                    metric_bindings={},
                    dimension_bindings={},
                    row=None,
                )
                if before.status != "evaluated" or after.status != "evaluated":
                    raise FormulaContractError("formula_shapley_evaluation_failed")
                assert before.value is not None and after.value is not None
                effect += weight * (after.value - before.value)
        output[metric_id] = effect
    return output


def _metric_ids(
    node: FormulaNode,
    metric_bindings: Mapping[str, str],
    dimension_bindings: Mapping[str, str],
) -> set[str]:
    if isinstance(node, MetricNode):
        return {metric_bindings.get(node.metric_id, node.metric_id)}
    if isinstance(node, (DimensionNode, ConstNode)):
        return set()
    if isinstance(node, (AddNode, MultiplyNode)):
        return set().union(
            *(
                _metric_ids(child, metric_bindings, dimension_bindings)
                for child in node.args
            )
        )
    if isinstance(node, (SubtractNode, DivideNode)):
        return _metric_ids(
            node.left, metric_bindings, dimension_bindings
        ) | _metric_ids(node.right, metric_bindings, dimension_bindings)
    if isinstance(node, SumByNode):
        return _metric_ids(node.expression, metric_bindings, dimension_bindings)
    if isinstance(node, RelationshipNode):
        nodes = (
            (node.left, node.right)
            if node.left is not None and node.right is not None
            else node.inputs
        )
        return set().union(
            *(
                _metric_ids(child, metric_bindings, dimension_bindings)
                for child in nodes
                if child is not None
            )
        )
    if isinstance(node, ProjectionNode):
        projected_metrics = dict(metric_bindings)
        projected_metrics.update(node.metric_bindings)
        projected_dimensions = dict(dimension_bindings)
        projected_dimensions.update(node.dimension_bindings)
        return _metric_ids(node.expression, projected_metrics, projected_dimensions)
    raise FormulaContractError("formula_ast_node_invalid")


def _contains_sum_by(node: FormulaNode) -> bool:
    if isinstance(node, SumByNode):
        return True
    if isinstance(node, (AddNode, MultiplyNode)):
        return any(_contains_sum_by(child) for child in node.args)
    if isinstance(node, (SubtractNode, DivideNode)):
        return _contains_sum_by(node.left) or _contains_sum_by(node.right)
    if isinstance(node, ProjectionNode):
        return _contains_sum_by(node.expression)
    return False


def _merge_failures(
    results: Sequence[_ExactEvaluation],
) -> _ExactEvaluation | None:
    missing_metrics = frozenset().union(
        *(result.missing_metric_ids for result in results)
    )
    missing_dimensions = frozenset().union(
        *(result.missing_dimension_ids for result in results)
    )
    if missing_metrics or missing_dimensions:
        return _ExactEvaluation(
            status="missing",
            missing_metric_ids=missing_metrics,
            missing_dimension_ids=missing_dimensions,
            reason="formula_input_missing",
        )
    for result in results:
        if result.status != "evaluated":
            return result
    return None


def _node_sequence(value: Any) -> tuple[FormulaNode, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise FormulaContractError("formula_ast_args_invalid")
    return tuple(validate_formula_ast(item) for item in value)


def _bindings(value: Any, error: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise FormulaContractError(error)
    bindings: list[tuple[str, str]] = []
    for source, target in value.items():
        source_id = _required_id(source, error)
        if isinstance(target, Mapping):
            target = target.get("runtime_metric_id")
        bindings.append((source_id, _required_id(target, error)))
    return tuple(sorted(bindings))


def _validated_factor_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values:
        raise FormulaContractError("formula_factor_ids_invalid")
    normalized = tuple(
        _required_id(value, "formula_factor_ids_invalid") for value in values
    )
    if len(normalized) != len(set(normalized)):
        raise FormulaContractError("formula_factor_ids_duplicated")
    return normalized


def _required_id(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FormulaContractError(error)
    return value


def _require_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise FormulaContractError("formula_ast_fields_invalid")


def _number(value: Any) -> Fraction | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Fraction(str(value))
    return None


def _nonnegative_fraction(value: Any, error: str) -> Fraction:
    result = _number(value)
    if result is None or result < 0:
        raise FormulaContractError(error)
    return result


def _nonnegative_float(value: Any, error: str) -> float:
    return _float_required(_nonnegative_fraction(value, error))


def _direction(
    value: Fraction,
) -> Literal["increase", "decrease", "flat"]:
    if value > 0:
        return "increase"
    if value < 0:
        return "decrease"
    return "flat"


def _float(value: Fraction | None) -> float | None:
    return float(value) if value is not None else None


def _float_required(value: Fraction | None) -> float:
    if value is None:
        raise FormulaContractError("formula_numeric_value_invalid")
    return float(value)
