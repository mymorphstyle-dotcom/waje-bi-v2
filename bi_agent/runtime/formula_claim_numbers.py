from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FormulaContributionDefinition:
    formula_id: str
    target_metric_id: str
    component_ids: tuple[str, ...]
    derivation_id: str


PAID_AMOUNT_THREE_FACTOR_DEFINITION = FormulaContributionDefinition(
    formula_id="paid_amount_frequency_ticket_size",
    target_metric_id="paid_amount",
    component_ids=(
        "paid_users",
        "paid_frequency",
        "avg_order_amount",
    ),
    derivation_id="multiplicative_shapley_v1",
)

THREE_FACTOR_COMPONENT_IDS = PAID_AMOUNT_THREE_FACTOR_DEFINITION.component_ids
FORMULA_CONTRIBUTION_TOTAL_FIELD = "formula_contribution_total"


FORMULA_COMPONENT_NUMBER_SEMANTICS = {
    **{
        f"{component_id}_contribution": (
            "three_factor_contribution",
            component_id,
        )
        for component_id in THREE_FACTOR_COMPONENT_IDS
    },
    **{
        f"{component_id}_contribution_share": (
            "three_factor_contribution_share",
            component_id,
        )
        for component_id in THREE_FACTOR_COMPONENT_IDS
    },
    FORMULA_CONTRIBUTION_TOTAL_FIELD: (
        "three_factor_effect_total",
        PAID_AMOUNT_THREE_FACTOR_DEFINITION.target_metric_id,
    ),
}


def formula_component_number_semantics(field: str) -> tuple[str, str] | None:
    return FORMULA_COMPONENT_NUMBER_SEMANTICS.get(field.lower())


def project_formula_claim_numbers(
    decomposition: Mapping[str, Any],
    *,
    definition: FormulaContributionDefinition = PAID_AMOUNT_THREE_FACTOR_DEFINITION,
) -> dict[str, Any]:
    contributions = {
        str(item.get("component_id") or ""): item
        for item in decomposition.get("core_factor_contributions") or ()
        if isinstance(item, Mapping)
    }
    if any(component_id not in contributions for component_id in definition.component_ids):
        return {}
    projected: dict[str, Any] = {}
    for component_id in definition.component_ids:
        contribution = contributions[component_id]
        value = contribution.get("contribution")
        if value is None:
            return {}
        projected[f"{component_id}_contribution"] = value
        share = contribution.get("contribution_share")
        if share is not None:
            projected[f"{component_id}_contribution_share"] = share
    total = decomposition.get("core_factor_effect_total")
    if total is None:
        return {}
    projected[FORMULA_CONTRIBUTION_TOTAL_FIELD] = total
    return projected
