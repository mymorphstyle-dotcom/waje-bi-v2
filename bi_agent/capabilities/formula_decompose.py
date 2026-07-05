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
    gaps = []
    for path in formula_paths:
        components = tuple(path.get("components", ()))
        missing = tuple(component for component in components if component not in available)
        record = {"formula_id": path.get("formula_id"), "components": components}
        if missing:
            gaps.append({**record, "missing_components": missing})
        else:
            covered.append(record)

    limitations = tuple(f"missing_formula_component:{gap['formula_id']}" for gap in gaps)
    if not covered and not gaps:
        limitations = ("no_formula_paths",)
    return make_evidence_envelope(
        "formula_decompose",
        evidence_type="accounting_contribution" if covered else "data_gap",
        strength="medium" if covered else "low",
        wording_limit="quantified" if covered else "missing_contract",
        typed_payload={"covered_paths": covered, "gaps": gaps},
        limitations=limitations,
        result_refs=result_refs,
    )


decompose_formula = formula_decompose
