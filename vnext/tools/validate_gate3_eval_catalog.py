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
PINNED_PARENT_POLICY_SHA256 = (
    "31bca083c1acf70f0980428f7b0ad5695373eac6d608ba703404caf736869add"
)
PINNED_PARENT_MINIMUM_CATALOG = {
    "reviewed_base_episodes": 36,
    "source_pool_minimums": {
        "real_user_language": 6,
        "expert_business_case": 6,
        "historical_failure": 6,
        "generated_business_world": 6,
        "adversarial_conversation": 6,
    },
    "counterfactual_siblings_per_episode": 3,
    "multi_turn_episodes": 12,
    "critical_risk_episodes": 6,
}
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
        ("business_world", "data_conditions", "*"),
        ("business_world", "data_conditions", "*", "description"),
        ("business_world", "data_conditions", "*", "discoverability"),
        ("business_world", "data_conditions", "*", "materiality"),
    ),
    "data_contract": (
        ("business_world", "available_contracts", "*"),
        ("business_world", "available_contracts", "*", "state"),
        ("business_world", "available_contracts", "*", "access_state"),
        (
            "business_world",
            "available_contracts",
            "*",
            "discoverability",
        ),
    ),
    "hidden_business_truth": (
        ("business_world", "truth_facts", "*"),
        ("business_world", "truth_facts", "*", "statement"),
    ),
    "conversation_history": (
        ("user_episode", "messages", "*"),
        ("user_episode", "messages", "*", "text"),
        ("user_episode", "messages", "*", "communication_function"),
        ("user_episode", "messages", "*", "trigger", "kind"),
        ("user_episode", "messages", "*", "trigger", "fallback"),
    ),
    "claim_strength_request": (
        ("acceptable_outcome", "claim_ceiling"),
        (
            "acceptable_outcome",
            "claim_targets",
            "*",
            "claim_ceiling",
        ),
        MESSAGE_TEXT,
    ),
}


def claim_ceiling_allows(overall: str, claim: str) -> bool:
    return claim in ALLOWED_CLAIMS_BY_CEILING.get(overall, set())


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


def episode_core(episode: Mapping[str, Any]) -> dict[str, Any]:
    core = {
        key: episode[key]
        for key in (
            "episode_id",
            "title",
            "source_pool",
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


def validate_counterfactual_materialization(
    episode: Mapping[str, Any],
    sibling: Mapping[str, Any],
) -> list[str]:
    """Replay one executable counterfactual and verify its exact digest."""

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

    operation_kind = operation["operation"]
    mutation_path = operation["path"]
    try:
        mutation_tokens = _json_pointer_tokens(mutation_path)
    except ValueError as error:
        return [str(error)]
    if not _mutation_path_allowed(
        sibling["mutation_dimension"], mutation_tokens
    ):
        return [
            "mutation dimension {} does not authorize path {}".format(
                sibling["mutation_dimension"], mutation_path
            )
        ]
    has_before = "before" in operation and operation["before"] is not None
    has_after = "after" in operation and operation["after"] is not None
    if operation_kind == "replace" and not (has_before and has_after):
        return ["replace requires non-null before and after values"]
    if operation_kind == "remove" and (
        not has_before or "after" in operation
    ):
        return ["remove requires before and forbids after"]
    if operation_kind == "add" and (
        "before" in operation or not has_after
    ):
        return ["add requires after and forbids before"]
    if operation_kind == "replace" and (
        isinstance(operation["before"], (dict, list))
        or isinstance(operation["after"], (dict, list))
    ):
        return [
            "replace must target one scalar authority field; use indexed add/remove for collections"
        ]

    materialized = copy.deepcopy(episode)
    try:
        tokens = mutation_tokens
        if tokens[0] == "counterfactual_siblings":
            raise ValueError(
                "counterfactual control metadata cannot be mutated"
            )
        parent, target = _resolve_pointer_parent(materialized, tokens)
        if isinstance(parent, list):
            if operation_kind == "add":
                index = _sequence_index(
                    target, len(parent), allow_end=True
                )
                parent.insert(index, copy.deepcopy(operation["after"]))
            else:
                index = _sequence_index(
                    target, len(parent), allow_end=False
                )
                actual_before = parent[index]
                if actual_before != operation["before"]:
                    raise ValueError(
                        "before value does not match the Episode"
                    )
                if operation_kind == "remove":
                    del parent[index]
                else:
                    parent[index] = copy.deepcopy(operation["after"])
        elif isinstance(parent, dict):
            if operation_kind == "add":
                if target in parent:
                    raise ValueError(
                        "add target already exists; use replace"
                    )
                parent[target] = copy.deepcopy(operation["after"])
            else:
                if target not in parent:
                    raise ValueError("mutation target does not exist")
                if parent[target] != operation["before"]:
                    raise ValueError(
                        "before value does not match the Episode"
                    )
                if operation_kind == "remove":
                    del parent[target]
                else:
                    parent[target] = copy.deepcopy(operation["after"])
        else:
            raise ValueError("mutation parent is not a container")
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
                "catalog_version": "gate3.behavior-eval.v2",
                "episodes": [materialized],
            }
        )
    ]
    materialized_findings.extend(
        _validate_episode_semantics(
            materialized,
            _load_json(TAXONOMY_PATH),
            validate_materializations=False,
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


def _policy_is_monotonic(policy: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    if (
        policy["parent_policy"]["canonical_sha256"]
        != PINNED_PARENT_POLICY_SHA256
    ):
        findings.append("parent policy hash does not match pinned v1")
    if (
        policy["parent_policy"]["minimum_catalog_snapshot"]
        != PINNED_PARENT_MINIMUM_CATALOG
    ):
        findings.append("parent policy floor snapshot does not match pinned v1")
    current = policy["minimum_catalog"]
    parent = policy["parent_policy"]["minimum_catalog_snapshot"]
    scalar_floors = (
        "reviewed_base_episodes",
        "counterfactual_siblings_per_episode",
        "multi_turn_episodes",
        "critical_risk_episodes",
    )
    for name in scalar_floors:
        if current[name] < parent[name]:
            findings.append(
                "policy floor {} decreased from {} to {}".format(
                    name, parent[name], current[name]
                )
            )
    for source_pool, parent_floor in parent[
        "source_pool_minimums"
    ].items():
        if current["source_pool_minimums"].get(source_pool, 0) < parent_floor:
            findings.append(
                "source floor {} decreased from {}".format(
                    source_pool, parent_floor
                )
            )
    return findings


def _validate_support_expectation(
    episode: Mapping[str, Any]
) -> list[str]:
    findings: list[str] = []
    episode_id = episode["episode_id"]
    expectation = episode["support_expectation"]
    outcome = episode["acceptable_outcome"]
    required = expectation["required_disposition"]
    if required not in outcome["allowed_dispositions"]:
        findings.append(
            "{} required disposition {} is not allowed".format(
                episode_id, required
            )
        )
    if expectation["contract_supported"] and required != "executable_design":
        findings.append(
            "{} contract_supported baseline must require executable_design".format(
                episode_id
            )
        )
    if required == "executable_design" and not outcome["valid_design_space"]:
        findings.append(
            "{} executable baseline requires a valid design family".format(
                episode_id
            )
        )
    boundary_codes = set(outcome.get("allowed_boundary_codes", []))
    authorized_codes = {
        authorization["boundary_code"]
        for authorization in expectation["boundary_authorizations"]
    }
    if boundary_codes != authorized_codes:
        findings.append(
            "{} boundary authorization mismatch: expected {}, got {}".format(
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
    }
    claim_targets_by_id = {
        target["claim_target_id"]: target
        for target in outcome.get("claim_targets", [])
    }
    for authorization in expectation["boundary_authorizations"]:
        if not claim_ceiling_allows(
            outcome["claim_ceiling"],
            authorization["maximum_claim_ceiling"],
        ):
            findings.append(
                "{} boundary {} ceiling exceeds outcome ceiling".format(
                    episode_id, authorization["boundary_code"]
                )
            )
        bound_claim_target_ids = set(
            authorization.get("claim_target_ids", [])
        )
        if claim_targets_by_id and not bound_claim_target_ids:
            findings.append(
                "{} boundary {} must bind claim target IDs".format(
                    episode_id, authorization["boundary_code"]
                )
            )
        unknown_claim_target_ids = (
            bound_claim_target_ids - set(claim_targets_by_id)
        )
        if unknown_claim_target_ids:
            findings.append(
                "{} boundary {} cites unknown claim targets {}".format(
                    episode_id,
                    authorization["boundary_code"],
                    sorted(unknown_claim_target_ids),
                )
            )
        for claim_target_id in (
            bound_claim_target_ids & set(claim_targets_by_id)
        ):
            if not claim_ceiling_allows(
                claim_targets_by_id[claim_target_id]["claim_ceiling"],
                authorization["maximum_claim_ceiling"],
            ):
                findings.append(
                    "{} boundary {} ceiling exceeds claim target {}".format(
                        episode_id,
                        authorization["boundary_code"],
                        claim_target_id,
                    )
                )
        for ref in authorization["allowed_when_refs"]:
            if ref not in world_refs:
                findings.append(
                    "{} boundary {} cites unknown fact {}".format(
                        episode_id, authorization["boundary_code"], ref
                    )
                )
    return findings


def _validate_episode_semantics(
    episode: Mapping[str, Any],
    taxonomy: Mapping[str, Any],
    *,
    validate_materializations: bool = True,
) -> list[str]:
    findings = _validate_support_expectation(episode)
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
        episode["business_world"]
    )
    for truth in episode["business_world"]["truth_facts"]:
        if truth["identifiability"] == "identifiable_from_world":
            support_refs = set(truth.get("support_refs", []))
            if (
                not support_refs
                or not truth.get("identification_basis")
                or support_refs - world_refs
                or support_refs - agent_discoverable_refs
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
        overall_ceiling = episode["acceptable_outcome"]["claim_ceiling"]
        if any(
            not claim_ceiling_allows(
                overall_ceiling, target["claim_ceiling"]
            )
            for target in claim_targets
        ):
            findings.append(
                "{} per-claim ceiling exceeds overall ceiling".format(
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
    for sibling in episode["counterfactual_siblings"]:
        if not sibling["mutation_operation"]["path"].startswith("/"):
            findings.append(
                "{} sibling {} requires an absolute mutation path".format(
                    episode_id, sibling["sibling_id"]
                )
            )
        if validate_materializations and (
            sibling["mutation_operation"].get("execution_status")
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
        policy["minimum_catalog"]["required_counterfactual_roles_per_episode"]
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
        "high_or_critical_risk_episode_count": sum(
            episode["decision_stakes"]["risk_level"] in {"high", "critical"}
            for episode in episodes
        ),
        "coverage": coverage,
        "authoring_gaps": {
            "missing_coverage": missing_coverage,
            "counterfactual_role_gaps": counterfactual_role_gaps,
        },
        "promotion_ready": False,
        "promotion_authority": "verify_gate3_e0.py",
    }


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
    findings.extend(_policy_is_monotonic(policy))

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
