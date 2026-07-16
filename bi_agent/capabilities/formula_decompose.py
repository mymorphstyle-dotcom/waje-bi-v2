from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def formula_decompose(
    formula_paths: Iterable[dict[str, Any]] = (),
    *,
    available_components: Iterable[str] = (),
    result_refs: tuple[str, ...] = (),
):
    available = set(available_components)
    covered = []
    reconciled = []
    unreconciled = []
    gaps = []
    for path in formula_paths:
        components = tuple(path.get("components", ()))
        declared_missing = tuple(
            dict.fromkeys(
                str(component)
                for component in (
                    *(path.get("missing_components") or ()),
                    *(path.get("missing_runtime_components") or ()),
                )
                if str(component)
            )
        )
        missing = tuple(
            dict.fromkeys(
                (
                    *declared_missing,
                    *(
                        component
                        for component in components
                        if component not in available
                    ),
                )
            )
        )
        missing_dimensions = tuple(
            str(dimension)
            for dimension in path.get("missing_dimensions") or ()
            if str(dimension)
        )
        formula_id = path.get("formula_id")
        reconciliation_status = str(
            path.get("reconciliation_status") or ""
        )
        contributions = path.get("contributions")
        residual = path.get("residual")
        has_quantified_reconciliation = (
            isinstance(contributions, dict)
            and bool(contributions)
            and isinstance(residual, (int, float))
            and not isinstance(residual, bool)
            and reconciliation_status in {"reconciled", "within_tolerance"}
            and str(path.get("verifier_status") or "") == "passed"
        )
        record = {
            "formula_id": formula_id,
            "components": components,
            "reconciliation_status": (
                reconciliation_status or "not_evaluated"
            ),
            **{
                key: path[key]
                for key in (
                    "candidate_role",
                    "candidate_status",
                    "candidate_rank",
                    "expression",
                    "ssot_node_id",
                    "path_role",
                    "target_component",
                    "contract_ref",
                    "matched_requested_components",
                    "launch_status",
                )
                if key in path
            },
        }
        if (
            missing
            or missing_dimensions
            or str(path.get("candidate_status") or "")
            in {"blocked", "degraded"}
        ):
            gaps.append(
                {
                    **record,
                    "missing_components": missing,
                    "missing_dimensions": missing_dimensions,
                }
            )
        else:
            covered.append(record)
            if has_quantified_reconciliation:
                reconciled.append(
                    {
                        **record,
                        "contributions": dict(contributions),
                        "residual": residual,
                    }
                )
            else:
                unreconciled.append(record)

    limitations = tuple(
        [
            f"missing_formula_component:{gap['formula_id']}"
            for gap in gaps
            if gap["missing_components"]
        ]
        + [
            f"missing_formula_dimension:{gap['formula_id']}"
            for gap in gaps
            if gap["missing_dimensions"]
        ]
        + [
            f"formula_reconciliation_missing:{path['formula_id']}"
            for path in unreconciled
        ]
    )
    if not covered and not gaps:
        limitations = ("no_formula_paths",)
    if covered and not gaps and not unreconciled:
        strength = "medium"
        wording_limit = "quantified"
    elif covered:
        strength = "low"
        wording_limit = "degraded"
    else:
        strength = "low"
        wording_limit = "missing_contract"

    return make_evidence_envelope(
        "formula_decompose",
        evidence_type="accounting_contribution" if covered else "data_gap",
        strength=strength,
        wording_limit=wording_limit,
        typed_payload={
            "covered_paths": covered,
            "reconciled_paths": reconciled,
            "unreconciled_paths": unreconciled,
            "gaps": gaps,
            "selection_state": (
                "verified_formula"
                if any(
                    path.get("candidate_role") == "primary_candidate"
                    for path in reconciled
                )
                else "candidate_only"
            ),
            "primary_formula": next(
                (
                    path
                    for path in reconciled
                    if path.get("candidate_role") == "primary_candidate"
                ),
                None,
            ),
        },
        limitations=limitations,
        result_refs=result_refs,
    )


decompose_formula = formula_decompose
