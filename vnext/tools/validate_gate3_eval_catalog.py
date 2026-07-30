#!/usr/bin/env python3
"""Validate Gate 3 behavior-first authoring catalogs.

This command proves authoring integrity and breadth. Promotion readiness is
owned by ``verify_gate3_e0.py`` because review, source, partition, grader,
calibration, and held-out authority live outside the Episode.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, FormatChecker
import yaml

from compile_gate3_eval_views import agent_accessible_world_refs


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
SCHEMA_PATH = EVAL_ROOT / "evaluation-episode.schema.json"
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
POLICY_SCHEMA_PATH = EVAL_ROOT / "gate3-eval-policy.schema.json"
TAXONOMY_PATH = EVAL_ROOT / "taxonomy" / "coverage-taxonomy.json"
AUTHORING_CATALOG_PATH = (
    EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
)
CASE_FILE_AUTHORITIES_PATH = (
    EVAL_ROOT / "case-files" / "case-file-authorities.json"
)
CASE_FILE_AUTHORITY_REF_PREFIX = (
    "vnext/evals/gate3/case-files/case-file-authorities.json#"
)
MISSING_CONTRACT_BACKLOG_PATH = (
    ROOT / "contracts" / "backlog" / "missing-contracts.yaml"
)
MISSING_CONTRACT_REF_PREFIX = (
    "vnext/contracts/backlog/missing-contracts.yaml#backlog."
)
ALLOWED_CLAIMS_BY_CEILING = {
    "definition_only": {"definition_only"},
    "data_quality_only": {"definition_only", "data_quality_only"},
    "descriptive": {
        "definition_only",
        "data_quality_only",
        "descriptive",
    },
    "associational": {
        "definition_only",
        "data_quality_only",
        "descriptive",
        "associational",
    },
    "accounting_attribution": {
        "definition_only",
        "data_quality_only",
        "descriptive",
        "accounting_attribution",
    },
    "causal": {
        "definition_only",
        "data_quality_only",
        "descriptive",
        "associational",
        "accounting_attribution",
        "causal",
    },
}
MESSAGE_TEXT = ("user_episode", "messages", "*", "text")
DESIGN_FIELD_PATHS = (
    ("acceptable_outcome", "valid_design_space", "*", "design_family"),
    ("acceptable_outcome", "valid_design_space", "*", "rationale"),
    (
        "acceptable_outcome",
        "valid_design_space",
        "*",
        "required_properties",
        "*",
    ),
)
MUTATION_PATH_TEMPLATES = {
    "wording": (MESSAGE_TEXT,),
    "decision_goal": (
        ("decision_stakes", "business_decision"),
        MESSAGE_TEXT,
    ),
    "scope": (
        ("acceptable_outcome", "must_preserve", "*"),
        MESSAGE_TEXT,
    ),
    "metric_definition": (
        (
            "business_world",
            "available_contracts",
            "*",
            "description",
        ),
        MESSAGE_TEXT,
    ),
    "time_semantics": (
        ("business_world", "evaluation_clock", "*"),
        (
            "business_world",
            "available_contracts",
            "*",
            "description",
        ),
        MESSAGE_TEXT,
    ),
    "window_or_baseline": (
        *DESIGN_FIELD_PATHS,
        ("business_world", "evaluation_clock", "*"),
        MESSAGE_TEXT,
    ),
    "observation_unit": (*DESIGN_FIELD_PATHS, MESSAGE_TEXT),
    "denominator": (*DESIGN_FIELD_PATHS, MESSAGE_TEXT),
    "exposure": (
        *DESIGN_FIELD_PATHS,
        ("business_world", "data_conditions", "*", "description"),
        MESSAGE_TEXT,
    ),
    "data_coverage": (
        ("business_world", "narrative"),
        ("business_world", "available_contracts", "*"),
        ("business_world", "data_conditions", "*"),
        ("business_world", "data_conditions", "*", "description"),
        ("business_world", "data_conditions", "*", "discoverability"),
        ("business_world", "data_conditions", "*", "materiality"),
        ("data_source_bindings", "*"),
        ("data_source_bindings", "*", "source_mode"),
        ("data_source_bindings", "*", "source_ref"),
        ("data_source_bindings", "*", "authority_ref"),
        ("data_source_bindings", "*", "required_identity_fields"),
        ("data_source_bindings", "*", "materialization_status"),
        ("data_source_bindings", "*", "agent_access", "surface"),
    ),
    "data_contract": (
        ("business_world", "available_contracts", "*"),
        (
            "business_world",
            "available_contracts",
            "*",
            "description",
        ),
        ("business_world", "available_contracts", "*", "state"),
        ("business_world", "available_contracts", "*", "access_state"),
        (
            "business_world",
            "available_contracts",
            "*",
            "discoverability",
        ),
        ("data_source_bindings", "*"),
        ("data_source_bindings", "*", "source_mode"),
        ("data_source_bindings", "*", "source_ref"),
        ("data_source_bindings", "*", "authority_ref"),
        ("data_source_bindings", "*", "required_identity_fields"),
        ("data_source_bindings", "*", "materialization_status"),
        ("data_source_bindings", "*", "agent_access", "surface"),
    ),
    "hidden_business_truth": (
        ("business_world", "truth_facts", "*"),
        ("business_world", "truth_facts", "*", "statement"),
        ("data_source_bindings", "*"),
    ),
    "conversation_history": (
        ("user_episode", "messages", "*"),
        ("user_episode", "messages", "*", "text"),
        ("user_episode", "messages", "*", "communication_function"),
        ("user_episode", "messages", "*", "trigger", "kind"),
        ("user_episode", "messages", "*", "trigger", "fallback"),
    ),
    "claim_strength_request": (
        (
            "acceptable_outcome",
            "claim_targets",
            "*",
            "design_claim_ceiling",
        ),
        MESSAGE_TEXT,
    ),
}
REPLACEMENT_REQUIRED_DIMENSIONS = {
    "decision_goal",
    "metric_definition",
    "scope",
}
BROAD_COLLECTION_MUTATION_PATHS = {
    ("user_episode", "messages"),
    ("business_world", "available_contracts"),
    ("business_world", "data_conditions"),
    ("business_world", "truth_facts"),
    ("acceptable_outcome", "valid_design_space"),
    ("acceptable_outcome", "must_preserve"),
    ("acceptable_outcome", "estimands"),
    ("acceptable_outcome", "claim_targets"),
    ("data_source_bindings",),
}


def claim_ceiling_allows(overall: str, claim: str) -> bool:
    return claim in ALLOWED_CLAIMS_BY_CEILING.get(overall, set())


def _identification_eligible_refs_at_turn(
    episode: Mapping[str, Any], *, visible_turn: int
) -> set[str]:
    world = episode["business_world"]
    future_release_refs = {
        event["affected_ref"]
        for event in world.get("scheduled_events", [])
        if event["event_type"] == "contract_release"
        and event.get("affected_ref")
        and event["after_user_turn"] > visible_turn
    }
    return {
        condition["condition_id"]
        for condition in world["data_conditions"]
        if condition["discoverability"] != "evaluator_only"
        and condition["condition_id"] not in future_release_refs
    } | {
        contract["contract_ref"]
        for contract in world["available_contracts"]
        if contract["state"] in {"available", "partial"}
        and contract["access_state"] == "accessible"
        and contract["discoverability"] != "evaluator_only"
        and contract["contract_ref"] not in future_release_refs
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def replacement_expectation_content_core(
    expectation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the content protected by a replacement-expectation digest."""

    return {
        key: expectation[key]
        for key in (
            "derivation",
            "source_intervention_sha256",
            "base_claim_refs",
            "variant_estimands",
            "variant_claim_targets",
            "variant_claim_cases",
            "variant_boundary_cases",
        )
    }


def counterfactual_intervention_sha256(
    sibling: Mapping[str, Any],
) -> str:
    """Bind replacement gold to the exact, ordered semantic patch set."""

    return canonical_sha256(
        sibling["mutation_operation"]["patches"]
    )


def episode_core(episode: Mapping[str, Any]) -> dict[str, Any]:
    core = {
        key: episode[key]
        for key in (
            "episode_id",
            "title",
            "source_pool",
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


def counterfactual_materialization_core(
    episode: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the business-authority content hashed for a materialized sibling.

    Counterfactual authoring metadata is excluded so the expected digest does
    not recursively contain its own ``materialized_sibling_sha256``.
    """

    core = episode_core(episode)
    del core["counterfactual_siblings"]
    return core


def _json_pointer_tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/") or pointer == "/":
        raise ValueError("mutation path must identify a nested Episode field")
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    ]


def _mutation_path_allowed(
    mutation_dimension: str, tokens: Sequence[str]
) -> bool:
    return any(
        len(tokens) == len(template)
        and all(
            expected == "*" or expected == actual
            for expected, actual in zip(template, tokens, strict=True)
        )
        for template in MUTATION_PATH_TEMPLATES[mutation_dimension]
    )


def _sequence_index(token: str, length: int, *, allow_end: bool) -> int:
    if token == "-" and allow_end:
        return length
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        raise ValueError("array path token must be a canonical index")
    index = int(token)
    upper_bound = length if allow_end else length - 1
    if index < 0 or index > upper_bound:
        raise ValueError("array path index is out of bounds")
    return index


def _resolve_pointer_parent(
    document: Any, tokens: Sequence[str]
) -> tuple[Any, str]:
    current = document
    for token in tokens[:-1]:
        if isinstance(current, list):
            current = current[
                _sequence_index(token, len(current), allow_end=False)
            ]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ValueError("mutation path does not resolve")
    return current, tokens[-1]


def materialize_counterfactual_episode(
    episode: Mapping[str, Any],
    sibling: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one exact counterfactual patch set to Episode authority."""

    materialized = copy.deepcopy(episode)
    for patch in sibling["mutation_operation"]["patches"]:
        tokens = _json_pointer_tokens(patch["path"])
        parent, target = _resolve_pointer_parent(materialized, tokens)
        operation_kind = patch["operation"]
        if isinstance(parent, list):
            if operation_kind == "add":
                index = _sequence_index(
                    target, len(parent), allow_end=True
                )
                parent.insert(index, copy.deepcopy(patch["after"]))
                continue
            index = _sequence_index(
                target, len(parent), allow_end=False
            )
            actual_before = parent[index]
            if actual_before != patch["before"]:
                raise ValueError("before value does not match the Episode")
            if operation_kind == "remove":
                del parent[index]
            else:
                parent[index] = copy.deepcopy(patch["after"])
            continue
        if not isinstance(parent, dict):
            raise ValueError("mutation parent is not a container")
        if operation_kind == "add":
            if target in parent:
                raise ValueError("add target already exists; use replace")
            parent[target] = copy.deepcopy(patch["after"])
            continue
        if target not in parent:
            raise ValueError("mutation target does not exist")
        if parent[target] != patch["before"]:
            raise ValueError("before value does not match the Episode")
        if operation_kind == "remove":
            del parent[target]
        else:
            parent[target] = copy.deepcopy(patch["after"])
    return materialized


def _physical_source_consistency_findings(
    base_episode: Mapping[str, Any],
    materialized_episode: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    base_bindings = {
        binding["binding_id"]: binding
        for binding in base_episode["data_source_bindings"]
    }
    materialized_bindings = {
        binding["binding_id"]: binding
        for binding in materialized_episode["data_source_bindings"]
    }
    for binding_id in sorted(
        set(base_bindings) & set(materialized_bindings)
    ):
        before = base_bindings[binding_id]
        after = materialized_bindings[binding_id]
        source_changed = (
            before["source_mode"],
            before["source_ref"],
        ) != (
            after["source_mode"],
            after["source_ref"],
        )
        authority_changed = (
            before["authority_ref"] != after["authority_ref"]
        )
        if source_changed and not authority_changed:
            findings.append(
                "physical source {} changed without a new authority identity".format(
                    binding_id
                )
            )
        if (
            before["source_mode"] != after["source_mode"]
            and (
                before["source_ref"] == after["source_ref"]
                or not authority_changed
            )
        ):
            findings.append(
                "physical source {} changed mode without atomic source and authority replacement".format(
                    binding_id
                )
            )
    return findings


def _replacement_claim_reachability_findings(
    base_episode: Mapping[str, Any],
    materialized_episode: Mapping[str, Any],
    expectation: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    episode_id = materialized_episode["episode_id"]
    bindings_by_id = {
        binding["binding_id"]: binding
        for binding in materialized_episode["data_source_bindings"]
    }
    user_turns = {
        message["turn"]
        for message in materialized_episode["user_episode"]["messages"]
    }
    truth_ids = {
        truth["truth_id"]
        for truth in materialized_episode["business_world"]["truth_facts"]
    }
    authorities_by_id = {
        authority["authority_id"]: authority
        for authority in _load_json(CASE_FILE_AUTHORITIES_PATH)[
            "authorities"
        ]
    }
    base_cases_by_id = {
        case["claim_target_id"]: case
        for case in base_episode["support_expectation"]["claim_cases"]
    }
    replacement_cases = [
        base_cases_by_id[ref["claim_target_id"]]
        for ref in expectation["base_claim_refs"]
        if ref["claim_target_id"] in base_cases_by_id
    ] + list(expectation["variant_claim_cases"])
    for case in replacement_cases:
        claim_target_id = case["claim_target_id"]
        evaluation_turn = case["evaluation_turn"]
        if evaluation_turn not in user_turns:
            findings.append(
                "{} replacement claim {} evaluation turn is not an actual user turn".format(
                    episode_id, claim_target_id
                )
            )
        visible_refs = agent_accessible_world_refs(
            materialized_episode,
            visible_turn=evaluation_turn,
        )
        unknown_observations = (
            set(case["required_observation_refs"]) - visible_refs
        )
        if unknown_observations:
            findings.append(
                "{} replacement claim {} requires unreachable observations {}".format(
                    episode_id,
                    claim_target_id,
                    sorted(unknown_observations),
                )
            )
        unknown_truths = set(case["oracle_truth_refs"]) - truth_ids
        if unknown_truths:
            findings.append(
                "{} replacement claim {} cites unknown oracle truths {}".format(
                    episode_id,
                    claim_target_id,
                    sorted(unknown_truths),
                )
            )
        for source_use in case["source_uses"]:
            binding_id = source_use["binding_id"]
            binding = bindings_by_id.get(binding_id)
            if binding is None:
                findings.append(
                    "{} replacement claim {} cites unknown source binding {}".format(
                        episode_id,
                        claim_target_id,
                        binding_id,
                    )
                )
                continue
            if (
                binding["agent_access"]["available_from_turn"]
                > evaluation_turn
            ):
                findings.append(
                    "{} replacement claim {} uses source {} before turn {}".format(
                        episode_id,
                        claim_target_id,
                        binding_id,
                        binding["agent_access"]["available_from_turn"],
                    )
                )
            if (
                binding["source_mode"] != "known_contract_gap"
                and binding["authority_ref"].startswith(
                    CASE_FILE_AUTHORITY_REF_PREFIX
                )
            ):
                authority_id = binding["authority_ref"].removeprefix(
                    CASE_FILE_AUTHORITY_REF_PREFIX
                )
                authority = authorities_by_id.get(authority_id)
                if (
                    authority is not None
                    and claim_target_id
                    not in authority["scope_claim_target_ids"]
                ):
                    findings.append(
                        "{} replacement claim {} is outside authority {} scope".format(
                            episode_id,
                            claim_target_id,
                            authority_id,
                        )
                    )
    return findings


def validate_replacement_expectation(
    episode: Mapping[str, Any],
    sibling: Mapping[str, Any],
    materialized_episode: Mapping[str, Any],
) -> list[str]:
    """Validate claim obligations for one materially revised user goal."""

    expectation = sibling.get("replacement_expectation")
    dimension = sibling["mutation_dimension"]
    relation = sibling["expected_relation"]
    if expectation is None:
        if (
            sibling["mutation_operation"].get("execution_status")
            == "executable_verified"
            and dimension in REPLACEMENT_REQUIRED_DIMENSIONS
        ):
            return [
                "substantive {} change requires replacement expectation".format(
                    dimension
                )
            ]
        return []
    if relation == "meaning_preserving":
        return [
            "meaning-preserving sibling cannot publish replacement expectation"
        ]

    findings: list[str] = []
    if (
        expectation["source_intervention_sha256"]
        != counterfactual_intervention_sha256(sibling)
    ):
        findings.append(
            "replacement expectation is bound to a different intervention"
        )
    actual_content_sha256 = canonical_sha256(
        replacement_expectation_content_core(expectation)
    )
    if expectation["content_sha256"] != actual_content_sha256:
        findings.append(
            "replacement expectation content digest mismatch"
        )

    base_targets_by_id = {
        target["claim_target_id"]: target
        for target in episode["acceptable_outcome"]["claim_targets"]
    }
    base_cases_by_id = {
        case["claim_target_id"]: case
        for case in episode["support_expectation"]["claim_cases"]
    }
    effects_by_id = {
        effect["claim_target_id"]: effect
        for effect in sibling.get("claim_effects", [])
    }
    expected_base_ref_ids = {
        claim_target_id
        for claim_target_id, effect in effects_by_id.items()
        if effect["claim_case_disposition"] != "supersede_or_omit"
    }
    base_refs = expectation["base_claim_refs"]
    base_ref_ids = {
        ref["claim_target_id"] for ref in base_refs
    }
    if len(base_refs) != len(base_ref_ids):
        findings.append(
            "replacement expectation base claim refs must be unique"
        )
    if base_ref_ids != expected_base_ref_ids:
        findings.append(
            "replacement expectation must exactly project non-superseded base claims"
        )
    for ref in base_refs:
        claim_target_id = ref["claim_target_id"]
        target = base_targets_by_id.get(claim_target_id)
        case = base_cases_by_id.get(claim_target_id)
        effect = effects_by_id.get(claim_target_id)
        if target is None or case is None or effect is None:
            findings.append(
                "replacement expectation cites unknown base claim {}".format(
                    claim_target_id
                )
            )
            continue
        if ref["estimand_id"] != target["estimand_id"]:
            findings.append(
                "replacement claim {} estimand identity drifted".format(
                    claim_target_id
                )
            )
        if ref["base_claim_case_sha256"] != canonical_sha256(case):
            findings.append(
                "replacement claim {} has stale base claim-case binding".format(
                    claim_target_id
                )
            )
        if ref["claim_effect_sha256"] != canonical_sha256(effect):
            findings.append(
                "replacement claim {} has stale claim-effect binding".format(
                    claim_target_id
                )
            )

    variant_estimands = expectation["variant_estimands"]
    variant_targets = expectation["variant_claim_targets"]
    variant_cases = expectation["variant_claim_cases"]
    variant_boundaries = expectation["variant_boundary_cases"]
    variant_estimand_ids = {
        estimand["estimand_id"] for estimand in variant_estimands
    }
    variant_target_ids = {
        target["claim_target_id"] for target in variant_targets
    }
    variant_case_ids = {
        case["claim_target_id"] for case in variant_cases
    }
    if (
        len(variant_estimand_ids) != len(variant_estimands)
        or len(variant_target_ids) != len(variant_targets)
        or len(variant_case_ids) != len(variant_cases)
    ):
        findings.append(
            "replacement variant estimands, targets and cases must be unique"
        )
    if variant_target_ids != variant_case_ids:
        findings.append(
            "replacement variant claim cases must exactly cover targets"
        )
    if {
        target["estimand_id"] for target in variant_targets
    } != variant_estimand_ids:
        findings.append(
            "replacement variant claims must exactly cover estimands"
        )
    if variant_target_ids & set(base_targets_by_id):
        findings.append(
            "variant-authored replacement claims must use new claim identities"
        )
    boundary_target_ids = {
        claim_target_id
        for boundary in variant_boundaries
        for claim_target_id in boundary.get("claim_target_ids", [])
    }
    if boundary_target_ids - variant_target_ids:
        findings.append(
            "replacement boundary cites a non-variant claim target"
        )
    authorized_boundary_codes = {
        boundary["boundary_code"] for boundary in variant_boundaries
    }
    claimed_boundary_codes = {
        boundary_code
        for case in variant_cases
        for boundary_code in case["boundary_codes"]
    }
    if claimed_boundary_codes - authorized_boundary_codes:
        findings.append(
            "replacement claim cites an unauthorized boundary code"
        )
    replacement_count = len(base_refs) + len(variant_targets)
    if replacement_count == 0:
        findings.append(
            "substantive change has no replacement claim"
        )
    if expectation["derivation"] == "base_claim_effect_projection" and (
        variant_estimands
        or variant_targets
        or variant_cases
        or variant_boundaries
    ):
        findings.append(
            "base claim projection cannot contain variant-authored gold"
        )
    if expectation["derivation"] == "variant_authored_gold" and (
        not variant_targets
    ):
        findings.append(
            "variant-authored replacement expectation lacks new claims"
        )
    findings.extend(
        _replacement_claim_reachability_findings(
            episode,
            materialized_episode,
            expectation,
        )
    )
    return findings


def validate_counterfactual_materialization(
    episode: Mapping[str, Any],
    sibling: Mapping[str, Any],
) -> list[str]:
    """Verify mechanical replay and digest identity for one counterfactual.

    This check does not approve the sibling's business semantics. Independent
    Episode review owns that decision.
    """

    operation = sibling["mutation_operation"]
    if operation.get("execution_status") != "executable_verified":
        return ["execution status is not executable_verified"]
    required_common = (
        "semantic_intervention_id",
        "materialized_sibling_sha256",
    )
    missing_common = [
        field for field in required_common if not operation.get(field)
    ]
    if missing_common:
        return [
            "executable mutation lacks {}".format(
                ", ".join(missing_common)
            )
        ]

    patches = operation["patches"]
    patch_paths = [patch["path"] for patch in patches]
    if len(patch_paths) != len(set(patch_paths)):
        return ["semantic intervention contains duplicate mutation paths"]
    parsed_patches: list[tuple[Mapping[str, Any], list[str]]] = []
    for patch in patches:
        mutation_path = patch["path"]
        try:
            mutation_tokens = _json_pointer_tokens(mutation_path)
        except ValueError as error:
            return [str(error)]
        if tuple(mutation_tokens) in BROAD_COLLECTION_MUTATION_PATHS:
            return [
                "broad collection mutation {} can hide multiple semantic changes".format(
                    mutation_path
                )
            ]
        if not _mutation_path_allowed(
            sibling["mutation_dimension"], mutation_tokens
        ):
            return [
                "mutation dimension {} does not authorize path {}".format(
                    sibling["mutation_dimension"], mutation_path
                )
            ]
        operation_kind = patch["operation"]
        has_before = "before" in patch and patch["before"] is not None
        has_after = "after" in patch and patch["after"] is not None
        if operation_kind == "replace" and not (has_before and has_after):
            return ["replace requires non-null before and after values"]
        if operation_kind == "remove" and (
            not has_before or "after" in patch
        ):
            return ["remove requires before and forbids after"]
        if operation_kind == "add" and (
            "before" in patch or not has_after
        ):
            return ["add requires after and forbids before"]
        parsed_patches.append((patch, mutation_tokens))

    try:
        for _, tokens in parsed_patches:
            if tokens[0] == "counterfactual_siblings":
                raise ValueError(
                    "counterfactual control metadata cannot be mutated"
                )
        materialized = materialize_counterfactual_episode(
            episode, sibling
        )
    except (KeyError, TypeError, ValueError) as error:
        return [str(error)]

    original_core = counterfactual_materialization_core(episode)
    materialized_core = counterfactual_materialization_core(materialized)
    if canonical_sha256(original_core) == canonical_sha256(materialized_core):
        return ["mutation does not change materialized Episode authority"]
    materialized_findings = [
        _format_error(error)
        for error in Draft202012Validator(
            _load_json(SCHEMA_PATH),
            format_checker=FormatChecker(),
        ).iter_errors(
            {
                "catalog_version": "gate3.behavior-eval.v4",
                "episodes": [materialized],
            }
        )
    ]
    materialized_findings.extend(
        _validate_episode_semantics(
            materialized,
            _load_json(TAXONOMY_PATH),
            validate_materializations=False,
            stale_claim_target_ids=set(
                sibling["affected_claim_target_ids"]
            ),
            replaceable_boundary_claim_target_ids={
                effect["claim_target_id"]
                for effect in sibling.get("claim_effects", [])
                if effect["boundary_codes"] in {"recompute", "clear"}
            },
            validate_counterfactual_claim_bindings=False,
        )
    )
    materialized_findings.extend(
        _physical_source_consistency_findings(
            episode,
            materialized,
        )
    )
    materialized_findings.extend(
        validate_replacement_expectation(
            episode,
            sibling,
            materialized,
        )
    )
    if materialized_findings:
        return [
            "materialized Episode is invalid: {}".format(finding)
            for finding in materialized_findings
        ]
    actual_sha256 = canonical_sha256(materialized_core)
    if operation["materialized_sibling_sha256"] != actual_sha256:
        return [
            "materialized sibling digest mismatch: expected {}, got {}".format(
                operation["materialized_sibling_sha256"],
                actual_sha256,
            )
        ]
    return []


def _format_error(error: Any) -> str:
    location = "/".join(str(part) for part in error.absolute_path)
    return "{}: {}".format(location or "<root>", error.message)


def _duplicates(values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _flatten_tags(
    episodes: Sequence[Mapping[str, Any]], dimension: str
) -> set[str]:
    return {
        value
        for episode in episodes
        for value in episode["coverage_tags"][dimension]
    }


def _validate_support_expectation(
    episode: Mapping[str, Any],
    *,
    stale_claim_target_ids: set[str] | None = None,
    replaceable_boundary_claim_target_ids: set[str] | None = None,
    validate_counterfactual_claim_bindings: bool = True,
) -> list[str]:
    findings: list[str] = []
    episode_id = episode["episode_id"]
    expectation = episode["support_expectation"]
    stale_claim_target_ids = stale_claim_target_ids or set()
    replaceable_boundary_claim_target_ids = (
        replaceable_boundary_claim_target_ids or set()
    )
    outcome = episode["acceptable_outcome"]
    source_bindings = episode["data_source_bindings"]
    binding_ids = [binding["binding_id"] for binding in source_bindings]
    if len(binding_ids) != len(set(binding_ids)):
        findings.append("{} source binding IDs must be unique".format(episode_id))
    bindings_by_id = {
        binding["binding_id"]: binding for binding in source_bindings
    }
    case_file_authorities = {
        authority["authority_id"]: authority
        for authority in _load_json(CASE_FILE_AUTHORITIES_PATH)[
            "authorities"
        ]
    }
    backlog_items = {
        item["backlog_id"]: item
        for item in yaml.safe_load(
            MISSING_CONTRACT_BACKLOG_PATH.read_text(encoding="utf-8")
        )["backlog"]
    }
    missing_contract_ids = {
        backlog_id
        for backlog_id, item in backlog_items.items()
        if item["data_contract_state"] == "missing_contract"
    }
    accepted_datasets = {
        "paid_order_success",
        "payment_final_outcome",
        "market_dashboard",
        "market_dashboard_channel",
        "gameplay",
        "gameplay_channel",
        "payment_order_bet_link",
        "external_event",
    }
    contracts_by_ref = {
        contract["contract_ref"]: contract
        for contract in episode["business_world"]["available_contracts"]
    }
    required_identity_fields_by_mode = {
        "frozen_real_snapshot": {
            "snapshot_release_ref",
            "coverage_watermark_ref",
            "query_result_ref",
        },
        "controlled_synthetic_fixture": {
            "fixture_version_ref",
            "fixture_content_sha256",
            "query_result_ref",
        },
        "known_contract_gap": {"authority_version_ref"},
    }
    for binding in source_bindings:
        expected_identity_fields = required_identity_fields_by_mode[
            binding["source_mode"]
        ]
        if set(binding["required_identity_fields"]) != expected_identity_fields:
            findings.append(
                "{} binding {} identity fields disagree with source mode".format(
                    episode_id, binding["binding_id"]
                )
            )
        if (
            binding["source_mode"] == "frozen_real_snapshot"
            and binding["source_ref"] not in accepted_datasets
        ):
            findings.append(
                "{} real source binding {} uses unknown dataset {}".format(
                    episode_id,
                    binding["binding_id"],
                    binding["source_ref"],
                )
            )
        if (
            binding["source_mode"] == "known_contract_gap"
            and (
                binding["authority_ref"] != binding["source_ref"]
                or
                not binding["authority_ref"].startswith(
                    MISSING_CONTRACT_REF_PREFIX
                )
                or binding["authority_ref"].removeprefix(
                    MISSING_CONTRACT_REF_PREFIX
                )
                not in missing_contract_ids
            )
        ):
            findings.append(
                "{} gap binding {} lacks resolvable backlog authority".format(
                    episode_id, binding["binding_id"]
                )
            )
        if (
            expectation["authoring_status"] == "claim_cases_complete"
            and binding["source_mode"] == "known_contract_gap"
        ):
            gap_contract = contracts_by_ref.get(binding["source_ref"])
            if (
                gap_contract is None
                or gap_contract["state"] != "missing"
            ):
                findings.append(
                    "{} gap binding {} must resolve to a missing world contract".format(
                        episode_id, binding["binding_id"]
                    )
                )
        if (
            expectation["authoring_status"] == "claim_cases_complete"
            and binding["source_mode"] != "known_contract_gap"
        ):
            authority_ref = binding["authority_ref"]
            if not authority_ref.startswith(
                CASE_FILE_AUTHORITY_REF_PREFIX
            ):
                findings.append(
                    "{} completed binding {} lacks case-file authority".format(
                        episode_id, binding["binding_id"]
                    )
                )
                continue
            authority_id = authority_ref.removeprefix(
                CASE_FILE_AUTHORITY_REF_PREFIX
            )
            authority = case_file_authorities.get(authority_id)
            if authority is None:
                findings.append(
                    "{} binding {} cites unknown case-file authority {}".format(
                        episode_id,
                        binding["binding_id"],
                        authority_id,
                    )
                )
                continue
            if authority["source_mode"] != binding["source_mode"]:
                findings.append(
                    "{} binding {} source mode disagrees with authority".format(
                        episode_id, binding["binding_id"]
                    )
                )
            if (
                binding["source_mode"] == "frozen_real_snapshot"
                and binding["source_ref"]
                not in authority.get("dataset_refs", [])
            ):
                findings.append(
                    "{} binding {} dataset is outside authority scope".format(
                        episode_id, binding["binding_id"]
                    )
                )
            if (
                binding["source_mode"] == "controlled_synthetic_fixture"
                and binding["source_ref"] != authority_id
            ):
                findings.append(
                    "{} binding {} fixture identity disagrees with authority".format(
                        episode_id, binding["binding_id"]
                    )
                )
            clock = episode["business_world"]["evaluation_clock"]
            authority_clock = authority["evaluation_clock"]
            if (
                clock["as_of_instant"]
                != authority_clock["as_of_instant"]
                or clock["default_business_timezone"]
                != authority_clock["business_timezone"]
            ):
                findings.append(
                    "{} binding {} evaluation clock disagrees with authority".format(
                        episode_id, binding["binding_id"]
                    )
                )
    for contract in episode["business_world"]["available_contracts"]:
        contract_ref = contract["contract_ref"]
        if (
            contract_ref.startswith(MISSING_CONTRACT_REF_PREFIX)
            and contract_ref.removeprefix(MISSING_CONTRACT_REF_PREFIX)
            not in missing_contract_ids
        ):
            findings.append(
                "{} contract {} cites unknown backlog item".format(
                    episode_id, contract_ref
                )
            )

    claim_targets_by_id = {
        target["claim_target_id"]: target
        for target in outcome["claim_targets"]
    }
    if (
        expectation["authoring_status"] == "claim_cases_complete"
        and validate_counterfactual_claim_bindings
    ):
        for sibling in episode["counterfactual_siblings"]:
            affected_claim_ids = set(
                sibling.get("affected_claim_target_ids", [])
            )
            if not affected_claim_ids:
                findings.append(
                    "{} sibling {} must affect at least one claim target".format(
                        episode_id, sibling["sibling_id"]
                    )
                )
            unknown_affected_claim_ids = (
                affected_claim_ids - set(claim_targets_by_id)
            )
            if unknown_affected_claim_ids:
                findings.append(
                    "{} sibling {} cites unknown affected claims {}".format(
                        episode_id,
                        sibling["sibling_id"],
                        sorted(unknown_affected_claim_ids),
                    )
                )
    claim_cases = expectation["claim_cases"]
    claim_cases_by_id = {
        case["claim_target_id"]: case for case in claim_cases
    }
    claim_case_ids = [case["claim_target_id"] for case in claim_cases]
    if len(claim_case_ids) != len(set(claim_case_ids)):
        findings.append("{} claim case IDs must be unique".format(episode_id))
    if expectation["authoring_status"] == "claim_cases_complete":
        if set(claim_case_ids) != set(claim_targets_by_id):
            findings.append(
                "{} complete claim cases must exactly cover claim targets".format(
                    episode_id
                )
            )
        for sibling in (
            episode["counterfactual_siblings"]
            if validate_counterfactual_claim_bindings
            else []
        ):
            affected_claim_ids = set(
                sibling["affected_claim_target_ids"]
            )
            claim_effects = sibling.get("claim_effects", [])
            claim_effect_ids = {
                effect["claim_target_id"] for effect in claim_effects
            }
            if len(claim_effects) != len(claim_effect_ids):
                findings.append(
                    "{} sibling {} claim effects must be unique".format(
                        episode_id, sibling["sibling_id"]
                    )
                )
            if claim_effect_ids != affected_claim_ids:
                findings.append(
                    "{} sibling {} claim effects must exactly cover affected claims".format(
                        episode_id, sibling["sibling_id"]
                    )
                )
            expected_unaffected = (
                set(claim_targets_by_id) - affected_claim_ids
            )
            if set(
                sibling.get("unaffected_claim_target_ids", [])
            ) != expected_unaffected:
                findings.append(
                    "{} sibling {} must explicitly preserve the exact unaffected claim set".format(
                        episode_id, sibling["sibling_id"]
                    )
                )
            relation = sibling["expected_relation"]
            removed_from_scope_profile = {
                "measurement_identity": "revise",
                "prior_evidence": "reject",
                "support_state": "not_applicable",
                "claim_case_disposition": "supersede_or_omit",
                "claim_ceiling": "not_applicable",
                "boundary_codes": "clear",
            }
            degraded_after_measurement_change_profiles = (
                {
                    "measurement_identity": "revise",
                    "prior_evidence": "revalidate",
                    "support_state": "degrade_or_recompute",
                    "claim_case_disposition": "degrade_or_omit",
                    "claim_ceiling": "lower_or_preserve",
                    "boundary_codes": "expand_or_preserve",
                },
                {
                    "measurement_identity": "revise",
                    "prior_evidence": "reject",
                    "support_state": "degrade_or_recompute",
                    "claim_case_disposition": "degrade_or_omit",
                    "claim_ceiling": "lower_or_preserve",
                    "boundary_codes": "expand_or_preserve",
                },
            )
            degraded_after_boundary_change_profile = {
                "measurement_identity": "preserve",
                "prior_evidence": "revalidate",
                "support_state": "degrade_or_recompute",
                "claim_case_disposition": "degrade_or_omit",
                "claim_ceiling": "lower_or_preserve",
                "boundary_codes": "expand_or_preserve",
            }
            recompute_after_measurement_change_profile = {
                "measurement_identity": "revise",
                "prior_evidence": "revalidate",
                "support_state": "recompute",
                "claim_case_disposition": "recompute",
                "claim_ceiling": "recompute",
                "boundary_codes": "recompute",
            }
            recompute_after_boundary_change_profile = {
                "measurement_identity": "preserve",
                "prior_evidence": "revalidate",
                "support_state": "recompute",
                "claim_case_disposition": "recompute",
                "claim_ceiling": "recompute",
                "boundary_codes": "recompute",
            }
            valid_effect_profiles = (
                (
                    {
                        "measurement_identity": "preserve",
                        "prior_evidence": "reusable",
                        "support_state": "preserve",
                        "claim_case_disposition": "preserve",
                        "claim_ceiling": "preserve",
                        "boundary_codes": "preserve",
                    },
                )
                if relation == "meaning_preserving"
                else (
                    (
                        degraded_after_boundary_change_profile,
                        recompute_after_boundary_change_profile,
                    )
                    if relation == "boundary_changing"
                    else (
                        recompute_after_measurement_change_profile,
                        *degraded_after_measurement_change_profiles,
                        removed_from_scope_profile,
                    )
                )
            )
            authority_effects = sibling["expected_authority_effects"]
            for field in ("prior_evidence", "claim_case_disposition"):
                if authority_effects[field] == "mixed":
                    observed_values = {
                        (
                            "degrade_or_omit"
                            if field == "claim_case_disposition"
                            and effect[field] == "supersede_or_omit"
                            else effect[field]
                        )
                        for effect in claim_effects
                    }
                    if len(observed_values) < 2:
                        findings.append(
                            "{} sibling {} declares mixed {} but claim effects are uniform".format(
                                episode_id,
                                sibling["sibling_id"],
                                field,
                            )
                        )
            for effect in claim_effects:
                claim_target_id = effect["claim_target_id"]
                base_case = claim_cases_by_id.get(claim_target_id)
                if (
                    base_case is None
                    or effect["base_claim_case_sha256"]
                    != canonical_sha256(base_case)
                ):
                    findings.append(
                        "{} sibling {} has stale base claim binding for {}".format(
                            episode_id,
                            sibling["sibling_id"],
                            claim_target_id,
                        )
                    )
                for field in (
                    "measurement_identity",
                    "prior_evidence",
                    "claim_case_disposition",
                ):
                    effect_summary_value = (
                        "degrade_or_omit"
                        if field == "claim_case_disposition"
                        and effect[field] == "supersede_or_omit"
                        else effect[field]
                    )
                    if (
                        authority_effects[field] != "mixed"
                        and effect_summary_value
                        != authority_effects[field]
                    ):
                        findings.append(
                            "{} sibling {} claim {} disagrees on {}".format(
                                episode_id,
                                sibling["sibling_id"],
                                claim_target_id,
                                field,
                            )
                        )
                if not any(
                    all(
                        effect[field] == expected
                        for field, expected in profile.items()
                    )
                    for profile in valid_effect_profiles
                ):
                    findings.append(
                        "{} sibling {} claim {} has invalid authority effect tuple".format(
                            episode_id,
                            sibling["sibling_id"],
                            claim_target_id,
                        )
                    )
    elif claim_cases:
        findings.append(
            "{} pending claim-case authoring must not publish partial claim cases".format(
                episode_id
            )
        )

    boundary_cases = expectation["boundary_cases"]
    boundary_codes = set(outcome.get("allowed_boundary_codes", []))
    authorized_codes = {
        boundary["boundary_code"] for boundary in boundary_cases
    }
    if boundary_codes != authorized_codes:
        findings.append(
            "{} boundary case mismatch: expected {}, got {}".format(
                episode_id,
                sorted(boundary_codes),
                sorted(authorized_codes),
            )
        )
    world_refs = {
        contract["contract_ref"]
        for contract in episode["business_world"]["available_contracts"]
    } | {
        condition["condition_id"]
        for condition in episode["business_world"]["data_conditions"]
    } | {binding["source_ref"] for binding in source_bindings}
    accessible_refs = agent_accessible_world_refs(episode)
    truths_by_id = {
        truth["truth_id"]: truth
        for truth in episode["business_world"]["truth_facts"]
    }
    truth_ids = set(truths_by_id)
    boundary_by_code = {
        boundary["boundary_code"]: boundary for boundary in boundary_cases
    }

    for boundary in boundary_cases:
        bound_claim_target_ids = set(
            boundary.get("claim_target_ids", [])
        )
        if claim_targets_by_id and not bound_claim_target_ids:
            findings.append(
                "{} boundary {} must bind claim target IDs".format(
                    episode_id, boundary["boundary_code"]
                )
            )
        unknown_claim_target_ids = (
            bound_claim_target_ids - set(claim_targets_by_id)
        )
        if unknown_claim_target_ids:
            findings.append(
                "{} boundary {} cites unknown claim targets {}".format(
                    episode_id,
                    boundary["boundary_code"],
                    sorted(unknown_claim_target_ids),
                )
            )
        active_bound_claim_target_ids = (
            bound_claim_target_ids
            & set(claim_targets_by_id)
            - stale_claim_target_ids
        )
        for claim_target_id in active_bound_claim_target_ids:
            if not claim_ceiling_allows(
                claim_targets_by_id[claim_target_id][
                    "design_claim_ceiling"
                ],
                boundary["maximum_claim_ceiling"],
            ):
                findings.append(
                    "{} boundary {} ceiling exceeds claim target {}".format(
                        episode_id,
                        boundary["boundary_code"],
                        claim_target_id,
                    )
                )
            claim_case = claim_cases_by_id.get(claim_target_id)
            if claim_case is not None:
                visible_boundary_refs = agent_accessible_world_refs(
                    episode,
                    visible_turn=claim_case["evaluation_turn"],
                )
                premature_refs = set(
                    boundary["required_observation_refs"]
                ) - visible_boundary_refs
                if premature_refs:
                    findings.append(
                        "{} boundary {} is not visible for claim {} at turn {}: {}".format(
                            episode_id,
                            boundary["boundary_code"],
                            claim_target_id,
                            claim_case["evaluation_turn"],
                            sorted(premature_refs),
                        )
                    )
        boundary_refs_may_be_replaced = (
            bool(bound_claim_target_ids)
            and bound_claim_target_ids.issubset(
                replaceable_boundary_claim_target_ids
            )
        )
        if boundary_refs_may_be_replaced:
            continue
        for ref in (
            boundary["allowed_when_refs"]
            + boundary["required_observation_refs"]
        ):
            if ref not in world_refs:
                findings.append(
                    "{} boundary {} cites unknown fact {}".format(
                        episode_id, boundary["boundary_code"], ref
                    )
                )
        inaccessible = (
            set(boundary["required_observation_refs"]) - accessible_refs
        )
        if inaccessible:
            findings.append(
                "{} boundary {} requires inaccessible observations {}".format(
                    episode_id,
                    boundary["boundary_code"],
                    sorted(inaccessible),
                )
            )

    for case in claim_cases:
        claim_target_id = case["claim_target_id"]
        if claim_target_id in stale_claim_target_ids:
            continue
        evaluation_turn = case["evaluation_turn"]
        user_turns = {
            message["turn"]
            for message in episode["user_episode"]["messages"]
        }
        if evaluation_turn not in user_turns:
            findings.append(
                "{} claim {} evaluation turn is not an actual user turn".format(
                    episode_id, claim_target_id
                )
            )
        if claim_target_id not in claim_targets_by_id:
            findings.append(
                "{} claim case cites unknown target {}".format(
                    episode_id, claim_target_id
                )
            )
            continue
        if case["disposition"]["resolution"] not in outcome[
            "permitted_resolution_kinds"
        ]:
            findings.append(
                "{} claim {} uses unpermitted resolution {}".format(
                    episode_id,
                    claim_target_id,
                    case["disposition"]["resolution"],
                )
            )
        if not claim_ceiling_allows(
            claim_targets_by_id[claim_target_id]["design_claim_ceiling"],
            case["effective_claim_ceiling"],
        ):
            findings.append(
                "{} claim case ceiling exceeds target {}".format(
                    episode_id, claim_target_id
                )
            )
        unknown_bindings = {
            item["binding_id"] for item in case["source_uses"]
        } - set(bindings_by_id)
        if unknown_bindings:
            findings.append(
                "{} claim {} cites unknown source bindings {}".format(
                    episode_id, claim_target_id, sorted(unknown_bindings)
                )
            )
        for source_use in case["source_uses"]:
            binding = bindings_by_id.get(source_use["binding_id"])
            if (
                binding
                and binding["agent_access"]["available_from_turn"]
                > evaluation_turn
            ):
                findings.append(
                    "{} claim {} uses source {} before turn {}".format(
                        episode_id,
                        claim_target_id,
                        source_use["binding_id"],
                        binding["agent_access"]["available_from_turn"],
                    )
                )
            if (
                binding
                and binding["source_mode"] == "known_contract_gap"
                and source_use["requirement"] != "boundary_probe"
            ):
                findings.append(
                    "{} claim {} uses gap {} as quantified evidence".format(
                        episode_id,
                        claim_target_id,
                        source_use["binding_id"],
                    )
                )
            if (
                binding
                and binding["source_mode"] != "known_contract_gap"
                and binding["authority_ref"].startswith(
                    CASE_FILE_AUTHORITY_REF_PREFIX
                )
            ):
                authority_id = binding["authority_ref"].removeprefix(
                    CASE_FILE_AUTHORITY_REF_PREFIX
                )
                authority = case_file_authorities.get(authority_id)
                if (
                    authority
                    and claim_target_id
                    not in authority["scope_claim_target_ids"]
                ):
                    findings.append(
                        "{} claim {} is outside authority {} scope".format(
                            episode_id,
                            claim_target_id,
                            authority_id,
                        )
                    )
        source_modes = {
            bindings_by_id[source_use["binding_id"]]["source_mode"]
            for source_use in case["source_uses"]
            if source_use["binding_id"] in bindings_by_id
        }
        expected_modes_by_applicability = {
            "real_snapshot_scope": {"frozen_real_snapshot"},
            "fixture_only_scope": {"controlled_synthetic_fixture"},
            "contract_boundary_scope": {"known_contract_gap"},
        }
        applicability = case["applicability"]
        expected_modes = expected_modes_by_applicability.get(applicability)
        if expected_modes is not None and source_modes != expected_modes:
            findings.append(
                "{} claim {} applicability {} disagrees with source modes {}".format(
                    episode_id,
                    claim_target_id,
                    applicability,
                    sorted(source_modes),
                )
            )
        if applicability == "mixed_scope" and len(source_modes) < 2:
            findings.append(
                "{} claim {} mixed scope requires multiple source modes".format(
                    episode_id, claim_target_id
                )
            )
        data_contract_state = case["support_state"][
            "data_contract_state"
        ]
        business_evidence_state = case["support_state"][
            "business_evidence_state"
        ]
        has_gap_source = "known_contract_gap" in source_modes
        gap_only = source_modes == {"known_contract_gap"}
        required_non_gap_sources = [
            source_use
            for source_use in case["source_uses"]
            if source_use["requirement"] == "required"
            and source_use["binding_id"] in bindings_by_id
            and bindings_by_id[source_use["binding_id"]]["source_mode"]
            != "known_contract_gap"
        ]
        if (
            case["disposition"]["resolution"] == "resolved_instance"
            and case["disposition"]["verifier"] == "accepted"
            and not required_non_gap_sources
        ):
            findings.append(
                "{} claim {} accepted resolution lacks required evidence source".format(
                    episode_id, claim_target_id
                )
            )
        if business_evidence_state in {"insufficient", "unidentifiable"} and (
            case["disposition"]["resolution"] == "resolved_instance"
            or case["disposition"]["verifier"] == "accepted"
            or case["disposition"]["settlement_precondition"] != "blocked"
        ):
            findings.append(
                "{} claim {} cannot resolve unidentifiable or insufficient evidence".format(
                    episode_id, claim_target_id
                )
            )
        if business_evidence_state == "not_evaluated" and (
            case["disposition"]["resolution"] != "omitted"
            or case["disposition"]["verifier"] != "not_run"
            or case["disposition"]["settlement_precondition"] != "blocked"
        ):
            findings.append(
                "{} claim {} not-evaluated evidence must remain omitted".format(
                    episode_id, claim_target_id
                )
            )
        if gap_only and (
            data_contract_state != "missing"
            or business_evidence_state
            not in {"insufficient", "unidentifiable", "not_evaluated"}
            or case["disposition"]["resolution"]
            not in {"typed_resolution_boundary", "omitted"}
            or case["disposition"]["verifier"]
            not in {"boundary_only", "rejected", "not_run"}
            or case["disposition"]["settlement_precondition"] != "blocked"
            or applicability != "contract_boundary_scope"
        ):
            findings.append(
                "{} claim {} gap-only support must remain an explicit blocked boundary".format(
                    episode_id, claim_target_id
                )
            )
        if has_gap_source and len(source_modes) > 1 and (
            data_contract_state == "supported"
            or business_evidence_state == "supported"
        ):
            findings.append(
                "{} claim {} cannot hide a contract gap behind supported state".format(
                    episode_id, claim_target_id
                )
            )
        if data_contract_state == "missing" and not has_gap_source:
            findings.append(
                "{} claim {} declares a missing contract without a gap source".format(
                    episode_id, claim_target_id
                )
            )
        if (
            case["disposition"]["verifier"] == "accepted"
            and case["disposition"]["resolution"] != "resolved_instance"
        ):
            findings.append(
                "{} claim {} accepted verifier requires a resolved instance".format(
                    episode_id, claim_target_id
                )
            )
        if (
            case["disposition"]["resolution"] == "typed_resolution_boundary"
            and case["disposition"]["verifier"] != "boundary_only"
        ):
            findings.append(
                "{} claim {} typed boundary requires boundary-only verification".format(
                    episode_id, claim_target_id
                )
            )
        if (
            data_contract_state
            in {"missing", "conflicting", "stale", "permission_blocked"}
            and business_evidence_state == "supported"
        ):
            findings.append(
                "{} claim {} cannot have supported business evidence with {} data contract".format(
                    episode_id,
                    claim_target_id,
                    data_contract_state,
                )
            )
        if case["disposition"][
            "settlement_precondition"
        ] == "eligible_for_future_settlement" and not (
            data_contract_state == "supported"
            and business_evidence_state == "supported"
            and case["disposition"]["resolution"] == "resolved_instance"
            and case["disposition"]["verifier"] == "accepted"
        ):
            findings.append(
                "{} claim {} has unsafe settlement eligibility".format(
                    episode_id, claim_target_id
                )
            )
        if (
            any(
                source_use["requirement"] == "required"
                for source_use in case["source_uses"]
            )
            and not case["required_observation_refs"]
        ):
            findings.append(
                "{} claim {} requires sources but no observable prerequisites".format(
                    episode_id, claim_target_id
                )
            )
        required_source_refs = {
            bindings_by_id[source_use["binding_id"]]["source_ref"]
            for source_use in case["source_uses"]
            if source_use["requirement"] in {"required", "boundary_probe"}
            and source_use["binding_id"] in bindings_by_id
        }
        missing_source_observations = required_source_refs - set(
            case["required_observation_refs"]
        )
        if missing_source_observations:
            findings.append(
                "{} claim {} omits source observations {}".format(
                    episode_id,
                    claim_target_id,
                    sorted(missing_source_observations),
                )
            )
        case_accessible_refs = agent_accessible_world_refs(
            episode, visible_turn=evaluation_turn
        )
        inaccessible = set(case["required_observation_refs"]) - (
            case_accessible_refs
        )
        if inaccessible:
            findings.append(
                "{} claim {} requires inaccessible observations {}".format(
                    episode_id, claim_target_id, sorted(inaccessible)
                )
            )
        unknown_truths = set(case["oracle_truth_refs"]) - truth_ids
        if unknown_truths:
            findings.append(
                "{} claim {} cites unknown oracle truths {}".format(
                    episode_id, claim_target_id, sorted(unknown_truths)
                )
            )
        claim_identification_refs = _identification_eligible_refs_at_turn(
            episode, visible_turn=evaluation_turn
        )
        for truth_ref in set(case["oracle_truth_refs"]) & truth_ids:
            truth = truths_by_id[truth_ref]
            if truth["identifiability"] != "identifiable_from_world":
                continue
            premature_support = set(truth.get("support_refs", [])) - (
                claim_identification_refs
            )
            if premature_support:
                findings.append(
                    "{} claim {} uses oracle truth {} before its identification support is visible: {}".format(
                        episode_id,
                        claim_target_id,
                        truth_ref,
                        sorted(premature_support),
                    )
                )
        if (
            case["disposition"]["resolution"] == "resolved_instance"
            and case["disposition"]["verifier"] == "accepted"
            and any(
                truths_by_id[truth_ref]["identifiability"]
                == "latent_unidentifiable"
                for truth_ref in case["oracle_truth_refs"]
                if truth_ref in truths_by_id
            )
        ):
            findings.append(
                "{} claim {} resolves from latent unidentifiable truth".format(
                    episode_id, claim_target_id
                )
            )
        unknown_boundaries = set(case["boundary_codes"]) - set(
            boundary_by_code
        )
        if unknown_boundaries:
            findings.append(
                "{} claim {} cites unknown boundaries {}".format(
                    episode_id, claim_target_id, sorted(unknown_boundaries)
                )
            )
        for code in set(case["boundary_codes"]) & set(boundary_by_code):
            if claim_target_id not in boundary_by_code[code].get(
                "claim_target_ids", []
            ):
                findings.append(
                    "{} claim {} boundary {} does not bind back to claim".format(
                        episode_id, claim_target_id, code
                    )
                )
        resolution = case["disposition"]["resolution"]
        verifier = case["disposition"]["verifier"]
        settlement = case["disposition"]["settlement_precondition"]
        if resolution == "typed_resolution_boundary" and (
            verifier != "boundary_only"
            or settlement != "blocked"
            or not case["boundary_codes"]
        ):
            findings.append(
                "{} typed boundary claim {} has invalid disposition".format(
                    episode_id, claim_target_id
                )
            )
        if resolution in {"clarification_required", "omitted"} and (
            verifier != "not_run" or settlement != "blocked"
        ):
            findings.append(
                "{} non-executed claim {} has invalid disposition".format(
                    episode_id, claim_target_id
                )
            )
    return findings


def _validate_episode_semantics(
    episode: Mapping[str, Any],
    taxonomy: Mapping[str, Any],
    *,
    validate_materializations: bool = True,
    stale_claim_target_ids: set[str] | None = None,
    replaceable_boundary_claim_target_ids: set[str] | None = None,
    validate_counterfactual_claim_bindings: bool = True,
) -> list[str]:
    findings = _validate_support_expectation(
        episode,
        stale_claim_target_ids=stale_claim_target_ids,
        replaceable_boundary_claim_target_ids=(
            replaceable_boundary_claim_target_ids
        ),
        validate_counterfactual_claim_bindings=(
            validate_counterfactual_claim_bindings
        ),
    )
    episode_id = episode["episode_id"]
    turns = [
        message["turn"] for message in episode["user_episode"]["messages"]
    ]
    if turns != sorted(set(turns)):
        findings.append(
            "{} user message turns must be unique and increasing".format(
                episode_id
            )
        )
    turn_set = set(turns)
    for event in episode["business_world"].get("scheduled_events", []):
        if event["after_user_turn"] not in turn_set:
            findings.append(
                "{} scheduled event {} follows a non-existent user turn".format(
                    episode_id, event["event_id"]
                )
            )
    messages = episode["user_episode"]["messages"]
    if messages[0]["trigger"]["kind"] != "initial":
        findings.append("{} first message trigger must be initial".format(episode_id))
    if any(
        message["trigger"]["kind"] == "initial"
        for message in messages[1:]
    ):
        findings.append(
            "{} only the first message may use initial trigger".format(
                episode_id
            )
        )
    clarification_mode = episode["acceptable_outcome"][
        "clarification_policy"
    ]["mode"]
    clarification_response_turns = [
        message["turn"]
        for message in messages
        if message["trigger"]["kind"] == "after_agent_clarification"
    ]
    if (
        clarification_mode == "must_ask_before_design"
        and episode["support_expectation"]["authoring_status"]
        == "claim_cases_complete"
    ):
        if not clarification_response_turns:
            findings.append(
                "{} complete must-ask Episode lacks a clarification response".format(
                    episode_id
                )
            )
        else:
            response_turn = min(clarification_response_turns)
            premature_claims = sorted(
                case["claim_target_id"]
                for case in episode["support_expectation"]["claim_cases"]
                if case["evaluation_turn"] < response_turn
            )
            if premature_claims:
                findings.append(
                    "{} evaluates claims before clarification {}".format(
                        episode_id, premature_claims
                    )
                )

    clock = episode["business_world"]["evaluation_clock"]
    try:
        ZoneInfo(clock["default_business_timezone"])
    except ZoneInfoNotFoundError:
        findings.append(
            "{} has unknown IANA timezone {}".format(
                episode_id, clock["default_business_timezone"]
            )
        )
    as_of = datetime.fromisoformat(clock["as_of_instant"])
    release_cutoff = datetime.fromisoformat(clock["release_cutoff_instant"])
    if release_cutoff > as_of:
        findings.append(
            "{} release cutoff cannot follow evaluation as-of".format(
                episode_id
            )
        )

    truth_ids = [
        truth["truth_id"]
        for truth in episode["business_world"]["truth_facts"]
    ]
    if len(truth_ids) != len(set(truth_ids)):
        findings.append("{} truth IDs must be unique".format(episode_id))
    world_ref_values = [
        contract["contract_ref"]
        for contract in episode["business_world"]["available_contracts"]
    ] + [
        condition["condition_id"]
        for condition in episode["business_world"]["data_conditions"]
    ]
    world_refs = set(world_ref_values)
    if len(world_ref_values) != len(world_refs):
        findings.append(
            "{} world contract and condition refs must be globally unique".format(
                episode_id
            )
        )
    agent_discoverable_refs = agent_accessible_world_refs(
        episode
    )
    identification_eligible_refs = {
        condition["condition_id"]
        for condition in episode["business_world"]["data_conditions"]
        if condition["discoverability"] != "evaluator_only"
    } | {
        contract["contract_ref"]
        for contract in episode["business_world"]["available_contracts"]
        if contract["state"] in {"available", "partial"}
        and contract["access_state"] == "accessible"
        and contract["discoverability"] != "evaluator_only"
    }
    for truth in episode["business_world"]["truth_facts"]:
        if truth["identifiability"] == "identifiable_from_world":
            support_refs = set(truth.get("support_refs", []))
            if (
                not support_refs
                or not truth.get("identification_basis")
                or support_refs - world_refs
                or support_refs - agent_discoverable_refs
                or support_refs - identification_eligible_refs
            ):
                findings.append(
                    "{} truth {} lacks valid identification support".format(
                        episode_id, truth["truth_id"]
                    )
                )

    estimands = episode["acceptable_outcome"].get("estimands", [])
    claim_targets = episode["acceptable_outcome"].get(
        "claim_targets", []
    )
    if estimands or claim_targets:
        estimand_ids = [item["estimand_id"] for item in estimands]
        claim_target_ids = [
            item["claim_target_id"] for item in claim_targets
        ]
        claimed_estimands = [
            item["estimand_id"] for item in claim_targets
        ]
        if len(estimand_ids) != len(set(estimand_ids)):
            findings.append(
                "{} estimand IDs must be unique".format(episode_id)
            )
        if len(claim_target_ids) != len(set(claim_target_ids)):
            findings.append(
                "{} claim target IDs must be unique".format(episode_id)
            )
        if set(claimed_estimands) != set(estimand_ids):
            findings.append(
                "{} claim targets must exactly cover estimands".format(
                    episode_id
                )
            )
        is_multi = len(estimand_ids) > 1
        tagged_multi = (
            "multi_estimand"
            in episode["coverage_tags"]["measurement_challenges"]
        )
        if is_multi != tagged_multi:
            findings.append(
                "{} multi_estimand tag must derive from estimand count".format(
                    episode_id
                )
            )

    for dimension, values in episode["coverage_tags"].items():
        allowed = set(taxonomy["dimensions"].get(dimension, []))
        unknown = sorted(set(values) - allowed)
        if unknown:
            findings.append(
                "{} has unknown {} coverage values: {}".format(
                    episode_id, dimension, ", ".join(unknown)
                )
            )
    expected_dimensions = set(taxonomy["dimensions"])
    actual_dimensions = set(episode["coverage_tags"])
    if expected_dimensions != actual_dimensions:
        findings.append(
            "{} coverage dimensions must exactly match taxonomy".format(
                episode_id
            )
        )

    expected_authoring_method = {
        "expert_business_case": "expert_authored",
        "historical_failure": "historical_failure_reconstruction",
        "generated_business_world": "controlled_world_generation",
        "adversarial_conversation": "adversarial_authoring",
    }.get(episode["source_pool"])
    actual_authoring_method = episode["provenance"]["authoring_method"]
    if (
        expected_authoring_method is not None
        and actual_authoring_method != expected_authoring_method
    ):
        findings.append(
            "{} source pool {} requires authoring method {}".format(
                episode_id,
                episode["source_pool"],
                expected_authoring_method,
            )
        )
    if episode["source_pool"] == "real_user_language" and (
        actual_authoring_method
        not in {"domain_interview", "redacted_production_trace"}
    ):
        findings.append(
            "{} real_user_language requires interview or trace provenance".format(
                episode_id
            )
        )
    executable_operations = [
        sibling["mutation_operation"]
        for sibling in episode["counterfactual_siblings"]
        if sibling["mutation_operation"].get("execution_status")
        == "executable_verified"
    ]
    intervention_ids = [
        operation["semantic_intervention_id"]
        for operation in executable_operations
    ]
    materialized_hashes = [
        operation["materialized_sibling_sha256"]
        for operation in executable_operations
    ]
    if len(intervention_ids) != len(set(intervention_ids)):
        findings.append(
            "{} executable siblings reuse semantic intervention ids".format(
                episode_id
            )
        )
    if len(materialized_hashes) != len(set(materialized_hashes)):
        findings.append(
            "{} executable siblings materialize duplicate authority states".format(
                episode_id
            )
        )
    allowed_relations_by_dimension = {
        "wording": {"meaning_preserving"},
        "conversation_history": {
            "meaning_preserving",
            "interaction_changing",
        },
        "data_contract": {"boundary_changing"},
        "data_coverage": {"boundary_changing"},
        "hidden_business_truth": {
            "boundary_changing",
            "interaction_changing",
        },
        "scope": {
            "measurement_changing",
            "boundary_changing",
            "interaction_changing",
        },
        "decision_goal": {"measurement_changing"},
        "metric_definition": {"measurement_changing"},
        "time_semantics": {"measurement_changing"},
        "window_or_baseline": {"measurement_changing"},
        "observation_unit": {"measurement_changing"},
        "denominator": {"measurement_changing"},
        "exposure": {"measurement_changing"},
        "claim_strength_request": {"measurement_changing"},
    }
    for sibling in episode["counterfactual_siblings"]:
        operation = sibling["mutation_operation"]
        expected_surfaces: set[str] = set()
        for patch in operation["patches"]:
            mutation_path = patch["path"]
            if not mutation_path.startswith("/"):
                findings.append(
                    "{} sibling {} requires an absolute mutation path".format(
                        episode_id, sibling["sibling_id"]
                    )
                )
            expected_surfaces.add(
                "user_message"
                if mutation_path.startswith("/user_episode/")
                else (
                    "semantic_contract"
                    if mutation_path.startswith(
                        (
                            "/business_world/available_contracts/",
                            "/data_source_bindings/",
                        )
                    )
                    else "world_fixture"
                )
            )
        if (
            len(expected_surfaces) > 1
            and operation["authority_surface"] != "composite_authority"
        ):
            findings.append(
                "{} sibling {} requires composite authority for surfaces {}".format(
                    episode_id,
                    sibling["sibling_id"],
                    sorted(expected_surfaces),
                )
            )
        elif (
            len(expected_surfaces) == 1
            and operation["authority_surface"] not in expected_surfaces
        ):
            findings.append(
                "{} sibling {} patches require authority surface {}".format(
                    episode_id,
                    sibling["sibling_id"],
                    next(iter(expected_surfaces)),
                )
            )
        relation = sibling["expected_relation"]
        allowed_relations = allowed_relations_by_dimension[
            sibling["mutation_dimension"]
        ]
        if relation not in allowed_relations:
            findings.append(
                "{} sibling {} relation {} is incompatible with mutation dimension {}".format(
                    episode_id,
                    sibling["sibling_id"],
                    relation,
                    sibling["mutation_dimension"],
                )
            )
        effects = sibling["expected_authority_effects"]
        if relation == "meaning_preserving" and effects != {
            "measurement_identity": "preserve",
            "prior_evidence": "reusable",
            "claim_case_disposition": "preserve",
        }:
            findings.append(
                "{} sibling {} meaning-preserving effects are inconsistent".format(
                    episode_id, sibling["sibling_id"]
                )
            )
        if relation in {"measurement_changing", "interaction_changing"} and (
            effects["measurement_identity"] != "revise"
            or effects["prior_evidence"] == "reusable"
            or effects["claim_case_disposition"] == "preserve"
        ):
            findings.append(
                "{} sibling {} change relation lacks authority revision effects".format(
                    episode_id, sibling["sibling_id"]
                )
            )
        if relation == "boundary_changing" and (
            effects["prior_evidence"] == "reusable"
            or effects["claim_case_disposition"] == "preserve"
        ):
            findings.append(
                "{} sibling {} boundary change lacks revalidation effects".format(
                    episode_id, sibling["sibling_id"]
                )
            )
        if validate_materializations and (
            operation.get("execution_status")
            == "executable_verified"
        ):
            findings.extend(
                "{} sibling {} {}".format(
                    episode_id, sibling["sibling_id"], finding
                )
                for finding in validate_counterfactual_materialization(
                    episode, sibling
                )
            )
    return findings


def _coverage_report(
    episodes: Sequence[Mapping[str, Any]],
    *,
    catalog_path: Path,
    taxonomy: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    source_counts = Counter(episode["source_pool"] for episode in episodes)
    coverage = {
        dimension: sorted(_flatten_tags(episodes, dimension))
        for dimension in taxonomy["dimensions"]
    }
    missing_coverage = {
        dimension: sorted(set(values) - set(coverage[dimension]))
        for dimension, values in taxonomy["dimensions"].items()
    }
    missing_coverage = {
        dimension: values
        for dimension, values in missing_coverage.items()
        if values
    }
    required_roles = set(
        policy["required_suite"]["required_counterfactual_roles_per_episode"]
    )
    counterfactual_role_gaps = {}
    for episode in episodes:
        observed_roles = {
            (
                sibling["expected_relation"]
                if sibling["expected_relation"]
                not in {"boundary_changing", "interaction_changing"}
                else "boundary_changing_or_interaction_changing"
            )
            for sibling in episode["counterfactual_siblings"]
        }
        gaps = sorted(required_roles - observed_roles)
        if gaps:
            counterfactual_role_gaps[episode["episode_id"]] = gaps
    return {
        "catalog_sha256": canonical_sha256(_load_json(catalog_path)),
        "schema_sha256": canonical_sha256(_load_json(SCHEMA_PATH)),
        "policy_sha256": canonical_sha256(policy),
        "taxonomy_sha256": canonical_sha256(taxonomy),
        "policy_version": policy["policy_version"],
        "episode_count": len(episodes),
        "source_pool_counts": dict(sorted(source_counts.items())),
        "multi_turn_episode_count": sum(
            len(episode["user_episode"]["messages"]) > 1
            for episode in episodes
        ),
        "critical_risk_episode_count": sum(
            episode["decision_stakes"]["risk_level"] == "critical"
            for episode in episodes
        ),
        "suite_binding_coverage": {
            "coverage_group_counts": dict(
                sorted(
                    Counter(
                        episode["suite_binding"]["coverage_group"]
                        for episode in episodes
                    ).items()
                )
            ),
            "factor_group_refs": sorted(
                {
                    factor
                    for episode in episodes
                    for factor in episode["suite_binding"][
                        "factor_group_refs"
                    ]
                }
            ),
            "question_family_refs": sorted(
                {
                    family
                    for episode in episodes
                    for family in episode["suite_binding"][
                        "question_family_refs"
                    ]
                }
            ),
            "source_modes": sorted(
                {
                    binding["source_mode"]
                    for episode in episodes
                    for binding in episode["data_source_bindings"]
                }
            ),
        },
        "coverage": coverage,
        "authoring_gaps": {
            "missing_coverage": missing_coverage,
            "counterfactual_role_gaps": counterfactual_role_gaps,
        },
        "promotion_ready": False,
        "promotion_authority": "verify_gate3_e0.py",
    }


def _validate_required_suite(
    episodes: Sequence[Mapping[str, Any]],
    *,
    taxonomy: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[str]:
    required = policy["required_suite"]
    findings: list[str] = []
    if len(episodes) != required["required_catalog_episodes"]:
        findings.append(
            "required catalog must contain exactly {} Episodes, got {}".format(
                required["required_catalog_episodes"], len(episodes)
            )
        )
    domains = {
        episode["suite_binding"]["business_domain"] for episode in episodes
    }
    if domains != {required["required_business_domain"]}:
        findings.append(
            "required business domain mismatch: {}".format(sorted(domains))
        )
    observed_groups = Counter(
        episode["suite_binding"]["coverage_group"] for episode in episodes
    )
    expected_groups = Counter(required["coverage_group_counts"])
    if observed_groups != expected_groups:
        findings.append(
            "coverage group counts mismatch: expected {}, got {}".format(
                dict(sorted(expected_groups.items())),
                dict(sorted(observed_groups.items())),
            )
        )
    observed_factors = {
        factor
        for episode in episodes
        for factor in episode["suite_binding"]["factor_group_refs"]
    }
    missing_factors = sorted(
        set(required["required_factor_group_refs"]) - observed_factors
    )
    if missing_factors:
        findings.append(
            "required factor group coverage missing: {}".format(
                missing_factors
            )
        )
    observed_families = {
        family
        for episode in episodes
        for family in episode["suite_binding"]["question_family_refs"]
    }
    missing_families = sorted(
        set(required["required_question_family_refs"]) - observed_families
    )
    if missing_families:
        findings.append(
            "required question family coverage missing: {}".format(
                missing_families
            )
        )
    observed_source_modes = {
        binding["source_mode"]
        for episode in episodes
        for binding in episode["data_source_bindings"]
    }
    missing_source_modes = sorted(
        set(required["required_source_modes"]) - observed_source_modes
    )
    if missing_source_modes:
        findings.append(
            "required source mode coverage missing: {}".format(
                missing_source_modes
            )
        )
    sibling_floor = required["counterfactual_siblings_per_episode"]
    required_counterfactual_roles = set(
        required["required_counterfactual_roles_per_episode"]
    )
    for episode in episodes:
        if len(episode["counterfactual_siblings"]) < sibling_floor:
            findings.append(
                "{} has fewer than {} counterfactual siblings".format(
                    episode["episode_id"], sibling_floor
                )
            )
        observed_counterfactual_roles = {
            (
                sibling["expected_relation"]
                if sibling["expected_relation"]
                not in {"boundary_changing", "interaction_changing"}
                else "boundary_changing_or_interaction_changing"
            )
            for sibling in episode["counterfactual_siblings"]
        }
        missing_counterfactual_roles = sorted(
            required_counterfactual_roles - observed_counterfactual_roles
        )
        if missing_counterfactual_roles:
            findings.append(
                "{} lacks required counterfactual roles {}".format(
                    episode["episode_id"],
                    missing_counterfactual_roles,
                )
            )
    multi_turn_count = sum(
        len(episode["user_episode"]["messages"]) > 1 for episode in episodes
    )
    if multi_turn_count < required["multi_turn_episodes"]:
        findings.append(
            "multi-turn Episode floor not met: {} < {}".format(
                multi_turn_count, required["multi_turn_episodes"]
            )
        )
    critical_count = sum(
        episode["decision_stakes"]["risk_level"] == "critical"
        for episode in episodes
    )
    if critical_count < required["critical_risk_episodes"]:
        findings.append(
            "critical-risk Episode floor not met: {} < {}".format(
                critical_count, required["critical_risk_episodes"]
            )
        )
    for dimension, values in taxonomy["dimensions"].items():
        missing = sorted(
            set(values) - _flatten_tags(episodes, dimension)
        )
        if missing:
            findings.append(
                "taxonomy coverage {} missing {}".format(dimension, missing)
            )
    return findings


def validate_catalog(
    catalog_path: Path, *, require_policy_ready: bool
) -> tuple[list[str], dict[str, Any]]:
    schema = _load_json(SCHEMA_PATH)
    policy = _load_json(POLICY_PATH)
    policy_schema = _load_json(POLICY_SCHEMA_PATH)
    taxonomy = _load_json(TAXONOMY_PATH)
    catalog = _load_json(catalog_path)
    findings = [
        _format_error(error)
        for error in Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(catalog)
    ]
    findings.extend(
        "policy {}".format(_format_error(error))
        for error in Draft202012Validator(policy_schema).iter_errors(policy)
    )
    if findings:
        return findings, {}
    if (
        policy["coverage_taxonomy"]["canonical_sha256"]
        != canonical_sha256(taxonomy)
    ):
        findings.append("policy coverage taxonomy hash is stale")
    schema_factor_refs = set(
        schema["$defs"]["suiteBinding"]["properties"][
            "factor_group_refs"
        ]["items"]["enum"]
    )
    policy_factor_refs = set(
        policy["required_suite"]["required_factor_group_refs"]
    )
    if schema_factor_refs != policy_factor_refs:
        findings.append(
            "policy factor groups differ from Episode schema"
        )
    schema_family_refs = set(
        schema["$defs"]["suiteBinding"]["properties"][
            "question_family_refs"
        ]["items"]["enum"]
    )
    policy_family_refs = set(
        policy["required_suite"]["required_question_family_refs"]
    )
    if schema_family_refs != policy_family_refs:
        findings.append(
            "policy question families differ from Episode schema"
        )
    schema_source_modes = set(
        schema["$defs"]["dataSourceBinding"]["properties"][
            "source_mode"
        ]["enum"]
    )
    policy_source_modes = set(
        policy["required_suite"]["required_source_modes"]
    )
    if schema_source_modes != policy_source_modes:
        findings.append(
            "policy source modes differ from Episode schema"
        )
    episodes = catalog["episodes"]
    duplicate_episode_ids = _duplicates(
        episode["episode_id"] for episode in episodes
    )
    duplicate_sibling_ids = _duplicates(
        sibling["sibling_id"]
        for episode in episodes
        for sibling in episode["counterfactual_siblings"]
    )
    if duplicate_episode_ids:
        findings.append(
            "duplicate episode IDs: {}".format(", ".join(duplicate_episode_ids))
        )
    if duplicate_sibling_ids:
        findings.append(
            "duplicate sibling IDs: {}".format(", ".join(duplicate_sibling_ids))
        )
    for episode in episodes:
        findings.extend(_validate_episode_semantics(episode, taxonomy))
    if catalog_path.resolve() == AUTHORING_CATALOG_PATH.resolve():
        findings.extend(
            _validate_required_suite(
                episodes,
                taxonomy=taxonomy,
                policy=policy,
            )
        )

    report = _coverage_report(
        episodes,
        catalog_path=catalog_path,
        taxonomy=taxonomy,
        policy=policy,
    )
    if require_policy_ready:
        findings.append(
            "authoring catalog cannot establish policy readiness; run verify_gate3_e0.py"
        )
    return findings, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--require-policy-ready", action="store_true")
    report_group = parser.add_mutually_exclusive_group()
    report_group.add_argument("--report", type=Path)
    report_group.add_argument("--check-report", type=Path)
    arguments = parser.parse_args()

    findings, report = validate_catalog(
        arguments.catalog,
        require_policy_ready=arguments.require_policy_ready,
    )
    if arguments.report is not None and report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if arguments.check_report is not None and report:
        if not arguments.check_report.exists():
            findings.append(
                "coverage report is missing: {}".format(
                    arguments.check_report
                )
            )
        elif _load_json(arguments.check_report) != report:
            findings.append(
                "coverage report is stale: {}".format(
                    arguments.check_report
                )
            )
    result = {
        "catalog": str(arguments.catalog),
        "status": "failed" if findings else "passed",
        "findings": findings,
        "coverage_report": report,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
