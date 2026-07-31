#!/usr/bin/env python3
"""Build Gate 3 derived artifacts from read-only authoring Episodes.

The authoring files remain editable business-world drafts. Review state,
partition assignment, grader selection, and promotion authority live in
external registries and manifests.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from validate_gate3_eval_catalog import (
    _format_error,
    _validate_episode_semantics,
    _validate_required_suite,
    business_world_independence_key,
    canonical_sha256,
    counterfactual_materialization_core,
    materialize_counterfactual_episode,
    replacement_expectation_content_core,
)


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
CANDIDATE_ROOT = EVAL_ROOT / "candidates"
EPISODE_SCHEMA_PATH = EVAL_ROOT / "evaluation-episode.schema.json"
CLAIM_TARGET_AUTHORITY_SCHEMA_PATH = (
    EVAL_ROOT / "claim-target-authority-registry.schema.json"
)
CLAIM_TARGET_AUTHORITY_PATH = (
    EVAL_ROOT / "registries" / "claim-target-authority-registry.json"
)
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
TAXONOMY_PATH = EVAL_ROOT / "taxonomy" / "coverage-taxonomy.json"
CATALOG_PATH = EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
CORPUS_REGISTRY_PATH = EVAL_ROOT / "registries" / "corpus-registry.json"
SOURCE_REGISTRY_PATH = EVAL_ROOT / "registries" / "source-registry.json"
WORLD_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "cross-gate-world-profiles.json"
)
REVIEW_PACKAGES_PATH = EVAL_ROOT / "promotion" / "review-packages.json"
AUTHORITY_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "authority-conformance-profiles.json"
)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: {!r}".format(key))
        result[key] = value
    return result


def _load_json_strict(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except ValueError as exc:
        raise ValueError("{}: {}".format(path.name, exc)) from exc


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _episode_core(episode: Mapping[str, Any]) -> dict[str, Any]:
    core = {
        key: episode[key]
        for key in (
            "episode_id",
            "title",
            "source_pool",
            "business_world_independence_key",
            "suite_binding",
            "data_source_bindings",
            "user_episode",
            "business_world",
            "decision_stakes",
            "support_expectation",
            "acceptable_outcome",
            "forbidden_outcomes",
            "counterfactual_siblings",
            "coverage_tags",
        )
    }
    core["review_provenance"] = {
        key: episode["provenance"][key]
        for key in ("source_record_ref", "authoring_batch_id")
    }
    return core


DESIGN_SPACE_POLICY = {
    "open_world_acceptance": True,
    "authored_examples_role": "illustrative_non_exhaustive",
    "acceptance_authority_refs": [
        "must_preserve",
        "must_investigate",
        "claim_targets",
        "support_expectation",
        "forbidden_outcomes",
    ],
    "disqualifier_codes": [
        "must_preserve_violated",
        "required_investigation_omitted",
        "claim_ceiling_exceeded",
        "forbidden_outcome_matched",
        "evidence_or_authority_boundary_violated",
    ],
}


def _bind_claim_target_kinds(
    targets: list[dict[str, Any]],
    kind_map: Mapping[str, str],
    *,
    label: str,
) -> None:
    target_ids = [target["claim_target_id"] for target in targets]
    if len(target_ids) != len(set(target_ids)):
        raise ValueError("{} repeats a claim target".format(label))
    if set(target_ids) != set(kind_map):
        raise ValueError(
            "{} claim target set differs from typed authority".format(label)
        )
    for target in targets:
        expected_kind = kind_map[target["claim_target_id"]]
        observed_kind = target.get("claim_target_kind", expected_kind)
        if observed_kind != expected_kind:
            raise ValueError(
                "{} claim target {} kind differs from typed authority".format(
                    label, target["claim_target_id"]
                )
            )
        target["claim_target_kind"] = expected_kind


def _enrich_episode(
    source_episode: Mapping[str, Any],
    binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    episode_id = source_episode["episode_id"]
    if binding is None:
        raise ValueError(
            "{} lacks claim target authority".format(episode_id)
        )
    episode = copy.deepcopy(source_episode)
    expected_independence_key = business_world_independence_key(episode)
    observed_independence_key = episode.get(
        "business_world_independence_key",
        expected_independence_key,
    )
    if observed_independence_key != expected_independence_key:
        raise ValueError(
            "{} business-world independence key drifted".format(episode_id)
        )
    episode["business_world_independence_key"] = expected_independence_key
    observed_design_policy = episode["acceptable_outcome"].get(
        "design_space_policy",
        DESIGN_SPACE_POLICY,
    )
    if observed_design_policy != DESIGN_SPACE_POLICY:
        raise ValueError(
            "{} open-world design policy drifted".format(episode_id)
        )
    episode["acceptable_outcome"]["design_space_policy"] = copy.deepcopy(
        DESIGN_SPACE_POLICY
    )
    _bind_claim_target_kinds(
        episode["acceptable_outcome"]["claim_targets"],
        binding["base_claim_target_kinds"],
        label=episode_id,
    )

    observed_replacements: set[str] = set()
    replacement_maps = binding["replacement_claim_target_kinds"]
    for sibling in episode["counterfactual_siblings"]:
        expectation = sibling.get("replacement_expectation")
        if expectation is None or not expectation["variant_claim_targets"]:
            continue
        sibling_id = sibling["sibling_id"]
        observed_replacements.add(sibling_id)
        kind_map = replacement_maps.get(sibling_id)
        if kind_map is None:
            raise ValueError(
                "{} lacks replacement claim target authority".format(
                    sibling_id
                )
            )
        _bind_claim_target_kinds(
            expectation["variant_claim_targets"],
            kind_map,
            label=sibling_id,
        )
        expectation["content_sha256"] = canonical_sha256(
            replacement_expectation_content_core(expectation)
        )
    if observed_replacements != set(replacement_maps):
        raise ValueError(
            "{} replacement claim target authority set drifted".format(
                episode_id
            )
        )

    for sibling in episode["counterfactual_siblings"]:
        materialized = materialize_counterfactual_episode(episode, sibling)
        sibling["mutation_operation"]["materialized_sibling_sha256"] = (
            canonical_sha256(
                counterfactual_materialization_core(materialized)
            )
        )
    return episode


def _load_candidates() -> list[dict[str, Any]]:
    policy = _load_json_strict(POLICY_PATH)
    schema = _load_json_strict(EPISODE_SCHEMA_PATH)
    claim_target_schema = _load_json_strict(
        CLAIM_TARGET_AUTHORITY_SCHEMA_PATH
    )
    claim_target_authority = _load_json_strict(
        CLAIM_TARGET_AUTHORITY_PATH
    )
    claim_target_schema_findings = [
        _format_error(error)
        for error in Draft202012Validator(
            claim_target_schema,
            format_checker=FormatChecker(),
        ).iter_errors(claim_target_authority)
    ]
    if claim_target_schema_findings:
        raise ValueError(
            "claim target authority validation failed:\n- {}".format(
                "\n- ".join(claim_target_schema_findings)
            )
        )
    target_bindings = {
        binding["episode_id"]: binding
        for binding in claim_target_authority["episode_bindings"]
    }
    if len(target_bindings) != len(
        claim_target_authority["episode_bindings"]
    ):
        raise ValueError("claim target authority repeats an Episode")
    taxonomy = _load_json_strict(TAXONOMY_PATH)
    required_paths = [
        CANDIDATE_ROOT / name
        for name in policy["required_suite"]["required_candidate_files"]
    ]
    observed_paths = set(CANDIDATE_ROOT.glob("*.json"))
    if observed_paths != set(required_paths):
        raise ValueError(
            "candidate input set differs from v4 allowlist: missing={}, "
            "unexpected={}".format(
                sorted(str(path) for path in set(required_paths) - observed_paths),
                sorted(str(path) for path in observed_paths - set(required_paths)),
            )
        )
    episodes: list[dict[str, Any]] = []
    findings: list[str] = []
    for path in required_paths:
        catalog = _load_json_strict(path)
        enriched_episodes = [
            _enrich_episode(
                episode,
                target_bindings.get(episode["episode_id"]),
            )
            for episode in catalog["episodes"]
        ]
        enriched_catalog = {
            "catalog_version": "gate3.behavior-eval.v5",
            "episodes": enriched_episodes,
        }
        findings.extend(
            "{} {}".format(path.name, _format_error(error))
            for error in Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(enriched_catalog)
        )
        episodes.extend(enriched_episodes)
    if set(target_bindings) != {
        episode["episode_id"] for episode in episodes
    }:
        findings.append(
            "claim target authority Episode set differs from candidates"
        )
    if not findings:
        for episode in episodes:
            findings.extend(
                _validate_episode_semantics(episode, taxonomy)
            )
        findings.extend(
            _validate_required_suite(
                episodes,
                taxonomy=taxonomy,
                policy=policy,
            )
        )
    duplicate_ids = [
        episode_id
        for episode_id in {
            episode["episode_id"] for episode in episodes
        }
        if sum(
            episode["episode_id"] == episode_id for episode in episodes
        )
        > 1
    ]
    if duplicate_ids:
        findings.append(
            "duplicate Episode IDs: {}".format(sorted(duplicate_ids))
        )
    if findings:
        raise ValueError(
            "candidate validation failed:\n- {}".format(
                "\n- ".join(findings)
            )
        )
    return episodes


def _corpus_registry(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "artifact_type": "corpus_registry",
        "artifact_version": "gate3.corpus-registry.v2",
        "registry_epoch": 2,
        "entries": [
            {
                "episode_id": episode["episode_id"],
                "episode_core_sha256": _sha256(_episode_core(episode)),
                "source_record_ref": episode["provenance"][
                    "source_record_ref"
                ],
                "authoring_batch_id": episode["provenance"][
                    "authoring_batch_id"
                ],
                "business_world_independence_key": episode[
                    "business_world_independence_key"
                ],
                "claim_target_kinds": sorted(
                    {
                        target["claim_target_kind"]
                        for target in episode["acceptable_outcome"][
                            "claim_targets"
                        ]
                    }
                ),
                "coverage_tags": episode["coverage_tags"],
                "product_grader_profile_ref": "GRADER-PRODUCT-BEHAVIOR-V1",
                "authority_profile_ref": "AUTHORITY-GATE3-BASE-V1",
                "world_profile_ref": "WORLD-PROFILE-{}".format(
                    episode["episode_id"]
                ),
            }
            for episode in sorted(
                episodes, key=lambda item: item["episode_id"]
            )
        ],
    }


def _world_profiles(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    entries = {
        entry["episode_id"]: entry
        for entry in _corpus_registry(episodes)["entries"]
    }
    return {
        "artifact_type": "cross_gate_world_profiles",
        "artifact_version": "gate3.cross-gate-world-profiles.v1",
        "registry_epoch": 1,
        "profiles": [
            {
                "profile_id": entries[episode["episode_id"]][
                    "world_profile_ref"
                ],
                "episode_id": episode["episode_id"],
                "episode_core_sha256": entries[episode["episode_id"]][
                    "episode_core_sha256"
                ],
                "world_id": episode["business_world"]["world_id"],
                "business_world_independence_key": episode[
                    "business_world_independence_key"
                ],
                "stage_profiles": {
                    "gate3": {
                        "status": "authoring",
                        "required_artifacts": [
                            "evaluation_episode_core",
                            "agent_world_view",
                            "evaluator_oracle_view",
                        ],
                    },
                    "gate4": {
                        "status": "pending",
                        "required_artifacts": [
                            "semantic_contract_bundle",
                            "frozen_data_snapshot",
                            "capability_availability_profile",
                            "result_oracle",
                        ],
                    },
                    "gate5": {
                        "status": "pending",
                        "required_artifacts": [
                            "claim_publication_profile",
                            "evidence_strength_oracle",
                        ],
                    },
                    "gate6": {
                        "status": "pending",
                        "required_artifacts": [
                            "workbench_observation_contract",
                            "customer_safe_projection_oracle",
                        ],
                    },
                    "gate7": {
                        "status": "pending",
                        "required_artifacts": [
                            "release_profile",
                            "replay_manifest",
                        ],
                    },
                },
            }
            for episode in sorted(
                episodes, key=lambda item: item["episode_id"]
            )
        ],
    }


def _authority_profiles(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    entries = {
        entry["episode_id"]: entry
        for entry in _corpus_registry(episodes)["entries"]
    }
    return {
        "artifact_type": "authority_conformance_profiles",
        "artifact_version": "gate3.authority-conformance.v1",
        "registry_epoch": 1,
        "profiles": [
            {
                "profile_id": "AUTHORITY-GATE3-BASE-V1",
                "authority_contract_version": (
                    "gate3.measurement-authority.epoch3"
                ),
                "required_invariant_ids": [
                    "source_measurement_identity_preserved",
                    "material_change_creates_revision",
                    "stale_result_rejected",
                    "evidence_claim_identity_compatible",
                    "settlement_preconditions_complete",
                ],
                "trace_profile_ref": "TRACE-GATE3-AUTHORITY-V1",
            }
        ],
        "bindings": [
            {
                "episode_id": episode["episode_id"],
                "episode_core_sha256": entries[episode["episode_id"]][
                    "episode_core_sha256"
                ],
                "world_profile_ref": entries[episode["episode_id"]][
                    "world_profile_ref"
                ],
                "profile_id": entries[episode["episode_id"]][
                    "authority_profile_ref"
                ],
            }
            for episode in sorted(
                episodes, key=lambda item: item["episode_id"]
            )
        ],
    }


def _review_packages(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    from compile_gate3_eval_views import compile_views

    source_status_by_id = {
        record["source_record_id"]: record["verification_status"]
        for record in _load_json_strict(SOURCE_REGISTRY_PATH)["records"]
    }
    entries = {
        entry["episode_id"]: entry
        for entry in _corpus_registry(episodes)["entries"]
    }
    packages: list[dict[str, Any]] = []
    for episode in sorted(episodes, key=lambda item: item["episode_id"]):
        entry = entries[episode["episode_id"]]
        views = compile_views(episode, entry)
        open_findings = [
            "truth_identifiability_pending:{}".format(truth["truth_id"])
            for truth in episode["business_world"]["truth_facts"]
            if truth["identifiability"] == "pending_independent_review"
        ]
        if not episode["acceptable_outcome"].get("estimands"):
            open_findings.append("estimand_contract_missing")
        if not episode["acceptable_outcome"].get("claim_targets"):
            open_findings.append("per_claim_ceiling_review_required")
        if (
            episode["support_expectation"]["authoring_status"]
            != "claim_cases_complete"
        ):
            open_findings.append("claim_case_authoring_pending")
        open_findings.extend(
            "counterfactual_not_executable:{}".format(
                sibling["sibling_id"]
            )
            for sibling in episode["counterfactual_siblings"]
            if sibling["mutation_operation"].get("execution_status")
            != "executable_verified"
        )
        open_findings.extend(
            "replacement_expectation_pending:{}".format(
                sibling["sibling_id"]
            )
            for sibling in episode["counterfactual_siblings"]
            if sibling["mutation_dimension"]
            in {"decision_goal", "metric_definition", "scope"}
            and "replacement_expectation" not in sibling
        )
        if (
            source_status_by_id.get(entry["source_record_ref"])
            != "verified"
        ):
            open_findings.append("source_provenance_pending")
        packages.append(
            {
                "package_id": "REVIEW-PACKAGE-{}".format(
                    episode["episode_id"]
                ),
                "episode_id": episode["episode_id"],
                "episode_core_sha256": entry["episode_core_sha256"],
                "authoring_episode_ref": (
                    "evals/gate3/catalog/gate3-authoring-candidates.json"
                    "#{}".format(episode["episode_id"])
                ),
                "source_record_ref": entry["source_record_ref"],
                "suite_binding": episode["suite_binding"],
                "data_source_bindings": episode[
                    "data_source_bindings"
                ],
                "evaluation_clock": episode["business_world"][
                    "evaluation_clock"
                ],
                "agent_world_view_sha256": views["agent_world_view"][
                    "view_sha256"
                ],
                "evaluator_oracle_view_sha256": views[
                    "evaluator_oracle_view"
                ]["view_sha256"],
                "business_review_scopes": [
                    "user wording and decision target",
                    "title, user, world, outcome and counterfactual semantic coherence",
                    "decision stakes and forbidden harms",
                    "business-world realism",
                    "required disposition and boundary usefulness",
                    "WAJEgame domain, factor and question-family binding",
                    "claim case source and contract boundary",
                    "source provenance",
                ],
                "measurement_review_scopes": [
                    "truth identifiability and support",
                    "estimand coherence across question, world, outcome and counterfactuals",
                    "valid design families",
                    "calendar and business-day semantics",
                    "counterfactual atomicity and relation",
                    "claim ceiling by estimand and claim target",
                    "WAJEgame domain, factor and question-family binding",
                    "claim case source and contract boundary",
                    "source provenance",
                ],
                "open_machine_findings": sorted(set(open_findings)),
                "required_independent_roles": [
                    "business_owner",
                    "measurement_reviewer",
                ],
            }
        )
    return {
        "package_version": "gate3.review-package.v1",
        "status": "awaiting_independent_review",
        "packages": packages,
    }


def _expected_artifacts() -> dict[Path, dict[str, Any]]:
    episodes = _load_candidates()
    artifacts = {
        CATALOG_PATH: {
            "catalog_version": "gate3.behavior-eval.v5",
            "episodes": episodes,
        }
    }
    artifacts[CORPUS_REGISTRY_PATH] = _corpus_registry(episodes)
    artifacts[AUTHORITY_PROFILES_PATH] = _authority_profiles(episodes)
    artifacts[WORLD_PROFILES_PATH] = _world_profiles(episodes)
    artifacts[REVIEW_PACKAGES_PATH] = _review_packages(episodes)
    return artifacts


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args()

    findings: list[str] = []
    for path, expected in _expected_artifacts().items():
        rendered = _render(expected)
        if arguments.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            continue
        if not path.exists():
            findings.append("missing generated artifact: {}".format(path))
            continue
        if path.read_text(encoding="utf-8") != rendered:
            findings.append("stale generated artifact: {}".format(path))

    if findings:
        print(json.dumps({"status": "failed", "findings": findings}, indent=2))
        return 1
    print(json.dumps({"status": "passed", "findings": []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
