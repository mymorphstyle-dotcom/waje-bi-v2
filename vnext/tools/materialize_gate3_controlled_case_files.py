#!/usr/bin/env python3
"""Materialize hash-bound Gate 3 controlled case files from authored Episodes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from validate_gate3_eval_catalog import (
    canonical_sha256,
    counterfactual_materialization_core,
    materialize_counterfactual_episode,
)


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
BUSINESS_CHAIN_PATH = (
    EVAL_ROOT / "candidates" / "wajegame_business_chain_episodes.json"
)
MEASUREMENT_REGRESSION_PATH = (
    EVAL_ROOT
    / "candidates"
    / "wajegame_measurement_regression_episodes.json"
)
CANDIDATE_PATHS = (
    BUSINESS_CHAIN_PATH,
    MEASUREMENT_REGRESSION_PATH,
)
AUTHORITIES_PATH = (
    EVAL_ROOT / "case-files" / "case-file-authorities.json"
)
FIXTURE_ROOT = EVAL_ROOT / "authority" / "fixtures"
FIXTURE_SCHEMA_PATH = (
    EVAL_ROOT / "authority" / "controlled-business-fixture.schema.json"
)
AUTHORITY_REF_PREFIX = (
    "vnext/evals/gate3/case-files/case-file-authorities.json#"
)
ARTIFACT_REF_PREFIX = "vnext/evals/gate3/authority/fixtures/"


def _render(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _fixture_slug(authority_id: str) -> str:
    return authority_id.removeprefix("FIXTURE-").removesuffix("-V1").lower()


def _case_candidates(
    episode: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None]]:
    candidates: list[
        tuple[str, Mapping[str, Any], Mapping[str, Any] | None]
    ] = [(episode["episode_id"], episode, None)]
    candidates.extend(
        (
            "{}:{}".format(episode["episode_id"], sibling["sibling_id"]),
            materialize_counterfactual_episode(episode, sibling),
            sibling,
        )
        for sibling in episode["counterfactual_siblings"]
        if sibling["mutation_operation"]["execution_status"]
        == "executable_verified"
    )
    return candidates


def _select_case(
    episodes: list[Mapping[str, Any]],
    authority_id: str,
) -> tuple[
    str,
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any] | None,
]:
    matches: list[
        tuple[
            str,
            Mapping[str, Any],
            Mapping[str, Any],
            Mapping[str, Any] | None,
        ]
    ] = []
    for episode in episodes:
        for case_ref, materialized, sibling in _case_candidates(episode):
            for binding in materialized["data_source_bindings"]:
                if binding["authority_ref"] == AUTHORITY_REF_PREFIX + authority_id:
                    matches.append(
                        (case_ref, materialized, binding, sibling)
                    )
    base_matches = [
        match for match in matches if ":" not in match[0]
    ]
    selected = base_matches or matches
    if not selected:
        raise ValueError(
            "No authored case binds authority {}".format(
                authority_id
            )
        )
    selected.sort(key=lambda item: item[0])
    return selected[0]


def _build_fixture(
    authority: Mapping[str, Any],
    case_ref: str,
    episode: Mapping[str, Any],
    binding: Mapping[str, Any],
    sibling: Mapping[str, Any] | None,
) -> dict[str, Any]:
    authority_id = authority["authority_id"]
    slug = _fixture_slug(authority_id)
    scoped_claim_ids = set(authority["scope_claim_target_ids"])
    claim_cases = [
        claim_case
        for claim_case in episode["support_expectation"]["claim_cases"]
        if claim_case["claim_target_id"] in scoped_claim_ids
    ]
    return {
        "artifact_type": "gate3_controlled_business_fixture",
        "artifact_version": "gate3.controlled-business-fixture.v1",
        "fixture_id": authority_id,
        "fixture_version_ref": "fixture://wajegame/gate3/{}/v1".format(
            slug
        ),
        "business_timezone": authority["evaluation_clock"][
            "business_timezone"
        ],
        "evaluation_clock": authority["evaluation_clock"],
        "case_identity": {
            "case_ref": case_ref,
            "episode_id": episode["episode_id"],
            "binding_id": binding["binding_id"],
            "authority_slot_id": authority["authority_slot_id"],
            "scope_claim_target_ids": authority[
                "scope_claim_target_ids"
            ],
        },
        "observable_contracts": [
            contract
            for contract in episode["business_world"][
                "available_contracts"
            ]
            if contract["access_state"] == "accessible"
            and contract["discoverability"] != "evaluator_only"
        ],
        "observable_conditions": [
            condition
            for condition in episode["business_world"]["data_conditions"]
            if condition["discoverability"] != "evaluator_only"
        ],
        "evaluator_oracle": {
            "truth_facts": episode["business_world"]["truth_facts"],
            "claim_cases": claim_cases,
            "replacement_expectation": (
                sibling.get("replacement_expectation")
                if sibling is not None
                else None
            ),
            "review_status": "pending_independent_review",
        },
        "agent_projection": {
            "expose": [
                "fixture_id",
                "fixture_version_ref",
                "business_timezone",
                "evaluation_clock",
                "case_identity",
                "observable_contracts",
                "observable_conditions",
            ],
            "hide": ["evaluator_oracle"],
        },
    }


def _replace_binding_status(
    value: Any,
    materialized_authority_ids: set[str],
) -> None:
    if isinstance(value, dict):
        authority_ref = value.get("authority_ref")
        if (
            isinstance(authority_ref, str)
            and authority_ref.startswith(AUTHORITY_REF_PREFIX)
            and authority_ref.split("#", 1)[1]
            in materialized_authority_ids
            and "materialization_status" in value
        ):
            value["materialization_status"] = "verified"
        for child in value.values():
            _replace_binding_status(child, materialized_authority_ids)
    elif isinstance(value, list):
        for child in value:
            _replace_binding_status(child, materialized_authority_ids)


def controlled_authority_ids(
    catalog: Mapping[str, Any],
) -> set[str]:
    authority_ids: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            authority_ref = value.get("authority_ref")
            if (
                isinstance(authority_ref, str)
                and authority_ref.startswith(AUTHORITY_REF_PREFIX)
            ):
                authority_ids.add(authority_ref.split("#", 1)[1])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(catalog)
    return authority_ids


def materialize(*, check: bool) -> list[str]:
    catalogs = {
        path: json.loads(path.read_text(encoding="utf-8"))
        for path in CANDIDATE_PATHS
    }
    episodes = [
        episode
        for catalog in catalogs.values()
        for episode in catalog["episodes"]
    ]
    authorities = json.loads(
        AUTHORITIES_PATH.read_text(encoding="utf-8")
    )
    fixture_validator = Draft202012Validator(
        json.loads(FIXTURE_SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    bound_authority_ids = set().union(
        *(
            controlled_authority_ids(catalog)
            for catalog in catalogs.values()
        )
    )
    target_authorities = [
        authority
        for authority in authorities["authorities"]
        if authority["authority_id"] in bound_authority_ids
        and authority["source_mode"] == "controlled_synthetic_fixture"
    ]
    findings: list[str] = []
    materialized_authority_ids: set[str] = set()
    for authority in target_authorities:
        authority_id = authority["authority_id"]
        case_ref, episode, binding, sibling = _select_case(
            episodes,
            authority_id,
        )
        fixture = _build_fixture(
            authority,
            case_ref,
            episode,
            binding,
            sibling,
        )
        schema_errors = sorted(
            fixture_validator.iter_errors(fixture),
            key=lambda error: list(error.absolute_path),
        )
        if schema_errors:
            findings.extend(
                "{} fixture schema violation at {}: {}".format(
                    authority_id,
                    "/".join(str(part) for part in error.absolute_path)
                    or "<root>",
                    error.message,
                )
                for error in schema_errors
            )
            continue
        artifact_name = "{}.v1.json".format(_fixture_slug(authority_id))
        artifact_path = FIXTURE_ROOT / artifact_name
        expected_text = _render(fixture)
        if check:
            if (
                not artifact_path.is_file()
                or artifact_path.read_text(encoding="utf-8")
                != expected_text
            ):
                findings.append(
                    "{} is missing or stale".format(artifact_path)
                )
        else:
            artifact_path.write_text(expected_text, encoding="utf-8")
        fixture_sha256 = canonical_sha256(fixture)
        expected_materialization = {
            "source_ref": authority_id,
            "artifact_ref": ARTIFACT_REF_PREFIX + artifact_name,
            "artifact_content_sha256": fixture_sha256,
            "identity_values": {
                "fixture_version_ref": fixture["fixture_version_ref"],
                "fixture_content_sha256": fixture_sha256,
                "query_result_ref": "result:{}".format(_fixture_slug(
                    authority_id
                )),
            },
        }
        if check:
            if authority.get("materializations") != [
                expected_materialization
            ]:
                findings.append(
                    "{} materialization metadata is stale".format(
                        authority_id
                    )
                )
        else:
            authority["materializations"] = [expected_materialization]
            authority["fixture_facts"] = [
                (
                    "The controlled fixture is mechanically materialized "
                    "from the authored Episode business world and claim-case "
                    "contract, independent of runtime output."
                ),
                (
                    "The artifact content is hash-bound; all truth facts "
                    "remain pending independent business and measurement "
                    "review."
                ),
            ]
        materialized_authority_ids.add(authority_id)
    if not check:
        for path, catalog in catalogs.items():
            _replace_binding_status(
                catalog,
                materialized_authority_ids,
            )
            for episode in catalog["episodes"]:
                for sibling in episode["counterfactual_siblings"]:
                    if (
                        sibling["mutation_operation"]["execution_status"]
                        != "executable_verified"
                    ):
                        continue
                    materialized = materialize_counterfactual_episode(
                        episode,
                        sibling,
                    )
                    sibling["mutation_operation"].pop(
                        "expected_materialized_sha256",
                        None,
                    )
                    sibling["mutation_operation"][
                        "materialized_sibling_sha256"
                    ] = canonical_sha256(
                        counterfactual_materialization_core(materialized)
                    )
            path.write_text(
                _render(catalog),
                encoding="utf-8",
            )
        AUTHORITIES_PATH.write_text(
            _render(authorities),
            encoding="utf-8",
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that artifacts and authority metadata are fresh.",
    )
    arguments = parser.parse_args()
    findings = materialize(check=arguments.check)
    print(
        json.dumps(
            {
                "status": "failed" if findings else "passed",
                "findings": findings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
