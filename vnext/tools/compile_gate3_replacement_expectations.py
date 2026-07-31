#!/usr/bin/env python3
"""Compile hash-bound replacement claims from reviewed base claim gold.

This compiler performs no business-semantic inference. It can only project
base claims that the reviewed counterfactual claim effects already retain for
recomputation or bounded handling. A counterfactual that needs a genuinely new
claim must receive separately reviewed ``variant_authored_gold``.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Mapping

from validate_gate3_eval_catalog import (
    REPLACEMENT_REQUIRED_DIMENSIONS,
    canonical_sha256,
    counterfactual_intervention_sha256,
    materialize_counterfactual_episode,
    replacement_expectation_content_core,
    validate_replacement_expectation,
)


VNEXT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = (
    VNEXT_ROOT
    / "evals"
    / "gate3"
    / "candidates"
    / "wajegame_launch_question_episodes.json"
)


class ReplacementCompilationError(ValueError):
    """The reviewed base gold cannot uniquely supply replacement claims."""


def derive_base_claim_replacement_expectation(
    episode: Mapping[str, Any],
    sibling: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the exact non-superseded base claims selected by claim effects."""

    if sibling["mutation_dimension"] not in REPLACEMENT_REQUIRED_DIMENSIONS:
        raise ReplacementCompilationError(
            "{} does not require a replacement expectation".format(
                sibling["sibling_id"]
            )
        )
    if (
        sibling["mutation_operation"].get("execution_status")
        != "executable_verified"
    ):
        raise ReplacementCompilationError(
            "{} is not executable".format(sibling["sibling_id"])
        )
    if (
        episode["support_expectation"]["authoring_status"]
        != "claim_cases_complete"
    ):
        raise ReplacementCompilationError(
            "{} has no reviewed base claim cases".format(
                episode["episode_id"]
            )
        )

    targets_by_id = {
        target["claim_target_id"]: target
        for target in episode["acceptable_outcome"]["claim_targets"]
    }
    cases_by_id = {
        case["claim_target_id"]: case
        for case in episode["support_expectation"]["claim_cases"]
    }
    retained_effects = [
        effect
        for effect in sibling.get("claim_effects", [])
        if effect["claim_case_disposition"] != "supersede_or_omit"
    ]
    if not retained_effects:
        raise ReplacementCompilationError(
            "{} has no non-superseded reviewed claim; author variant gold".format(
                sibling["sibling_id"]
            )
        )

    base_claim_refs: list[dict[str, Any]] = []
    for effect in retained_effects:
        claim_target_id = effect["claim_target_id"]
        target = targets_by_id.get(claim_target_id)
        case = cases_by_id.get(claim_target_id)
        if target is None or case is None:
            raise ReplacementCompilationError(
                "{} lacks reviewed target/case {}".format(
                    sibling["sibling_id"],
                    claim_target_id,
                )
            )
        base_claim_refs.append(
            {
                "claim_target_id": claim_target_id,
                "estimand_id": target["estimand_id"],
                "base_claim_case_sha256": canonical_sha256(case),
                "claim_effect_sha256": canonical_sha256(effect),
            }
        )

    expectation: dict[str, Any] = {
        "derivation": "base_claim_effect_projection",
        "source_intervention_sha256": (
            counterfactual_intervention_sha256(sibling)
        ),
        "base_claim_refs": base_claim_refs,
        "variant_estimands": [],
        "variant_claim_targets": [],
        "variant_claim_cases": [],
        "variant_boundary_cases": [],
    }
    expectation["content_sha256"] = canonical_sha256(
        replacement_expectation_content_core(expectation)
    )
    return expectation


def compile_catalog_replacement_expectations(
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a copy with mechanically derivable expectations and open gaps."""

    compiled = copy.deepcopy(catalog)
    gaps: list[str] = []
    for episode in compiled["episodes"]:
        for sibling in episode["counterfactual_siblings"]:
            if (
                sibling["mutation_dimension"]
                not in REPLACEMENT_REQUIRED_DIMENSIONS
                or sibling["mutation_operation"].get("execution_status")
                != "executable_verified"
            ):
                continue
            existing = sibling.get("replacement_expectation")
            if (
                existing is not None
                and existing.get("derivation") == "variant_authored_gold"
            ):
                materialized = materialize_counterfactual_episode(
                    episode, sibling
                )
                findings = validate_replacement_expectation(
                    episode, sibling, materialized
                )
                gaps.extend(
                    "{} {}".format(sibling["sibling_id"], finding)
                    for finding in findings
                )
                continue
            try:
                sibling["replacement_expectation"] = (
                    derive_base_claim_replacement_expectation(
                        episode,
                        sibling,
                    )
                )
            except ReplacementCompilationError as error:
                gaps.append(str(error))
    return compiled, gaps


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _render(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write mechanically compiled expectations to the catalog",
    )
    args = parser.parse_args()

    current = _load_json(args.catalog)
    compiled, gaps = compile_catalog_replacement_expectations(current)
    if gaps:
        for gap in gaps:
            print("replacement-gap: {}".format(gap))
        return 1
    rendered = _render(compiled)
    if args.write:
        args.catalog.write_text(rendered, encoding="utf-8")
        return 0
    if rendered != args.catalog.read_text(encoding="utf-8"):
        print("replacement expectations are stale or missing")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
