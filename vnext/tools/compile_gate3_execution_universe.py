#!/usr/bin/env python3
"""Compile Gate 3's exact execution universe and fail closed on missing authority."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

try:
    from tools.validate_gate3_eval_catalog import (
        materialize_counterfactual_episode,
    )
except ModuleNotFoundError:  # direct execution from vnext/tools
    from validate_gate3_eval_catalog import (
        materialize_counterfactual_episode,
    )


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
CATALOG_PATH = EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
PARAPHRASE_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-paraphrase-authority.schema.json"
)
PARAPHRASE_REGISTRY_PATH = (
    EVAL_ROOT / "registries" / "paraphrase-authority-registry.json"
)
OPERATOR_REGISTRY_PATH = (
    EVAL_ROOT / "registries" / "mutation-operator-registry.json"
)
SCENARIO_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-operator-scenario-authority.schema.json"
)
SCENARIO_REGISTRY_PATH = (
    EVAL_ROOT / "registries" / "operator-scenario-authority-registry.json"
)
TRACE_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "execution-trace-profiles.json"
)
GRADER_REGISTRY_PATH = EVAL_ROOT / "registries" / "grader-registry.json"
SOURCE_RUN_MANIFEST_PATH = EVAL_ROOT / "manifests" / "run-manifest.json"
HELD_OUT_MANIFEST_PATH = (
    EVAL_ROOT / "manifests" / "protected-held-out-manifest.json"
)
READINESS_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-execution-universe-readiness.schema.json"
)
READINESS_PATH = (
    EVAL_ROOT / "manifests" / "execution-universe-readiness.json"
)
EXECUTION_MANIFEST_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-execution-manifest.schema.json"
)

COMPILER_RELEASE_PATHS = (
    Path(__file__).resolve(),
    Path(__file__).resolve().parent / "validate_gate3_eval_catalog.py",
    READINESS_SCHEMA_PATH,
    PARAPHRASE_SCHEMA_PATH,
    SCENARIO_SCHEMA_PATH,
    EXECUTION_MANIFEST_SCHEMA_PATH,
)

AUTOMATIC_OPERATOR_BY_RELATION = {
    "meaning_preserving": "meaning_preserving_case_mutation",
    "measurement_changing": "material_semantic_change",
    "boundary_changing": "boundary_or_interaction_change",
    "interaction_changing": "boundary_or_interaction_change",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def compiler_release_sha256() -> str:
    return canonical_sha256(
        {
            str(path.relative_to(ROOT)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in COMPILER_RELEASE_PATHS
        }
    )


def schema_findings(value: Any, schema_path: Path) -> list[str]:
    return [
        "{}: {}".format(
            "/".join(str(part) for part in error.absolute_path) or "<root>",
            error.message,
        )
        for error in Draft202012Validator(
            load_json(schema_path)
        ).iter_errors(value)
    ]


def case_variants(
    episode: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    variants: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("base", {"kind": "base"}, dict(episode))
    ]
    for sibling in episode["counterfactual_siblings"]:
        variants.append(
            (
                sibling["sibling_id"],
                {
                    "kind": "counterfactual",
                    "sibling_id": sibling["sibling_id"],
                    "materialized_sibling_sha256": sibling[
                        "mutation_operation"
                    ]["materialized_sibling_sha256"],
                },
                materialize_counterfactual_episode(episode, sibling),
            )
        )
    return variants


def paraphrase_authority_ref(
    episode_id: str,
    case_variant_ref: str,
    paraphrase_index: int,
) -> str:
    variant_token = (
        "BASE"
        if case_variant_ref == "base"
        else case_variant_ref.rsplit("-", 1)[-1]
    )
    return "PARA-{}-{}-{}".format(
        episode_id,
        variant_token,
        paraphrase_index,
    )


def coordinate_core(
    *,
    episode_id: str,
    case_variant_ref: str,
    case_variant: Mapping[str, Any],
    risk_level: str,
    lane: str,
    paraphrase_index: int,
    repeat_index: int,
) -> dict[str, Any]:
    wording_authority_ref = (
        f"episode:{episode_id}:{case_variant_ref}:base"
        if paraphrase_index == 0
        else paraphrase_authority_ref(
            episode_id,
            case_variant_ref,
            paraphrase_index,
        )
    )
    identity = {
        "episode_id": episode_id,
        "case_variant_ref": case_variant_ref,
        "case_variant": case_variant,
        "risk_level": risk_level,
        "lane": lane,
        "paraphrase_index": paraphrase_index,
        "repeat_index": repeat_index,
        "visible_turn": 1,
        "wording_authority_ref": wording_authority_ref,
    }
    digest = canonical_sha256(identity)
    return {
        **identity,
        "execution_cell_id": "CELL-{}".format(digest[:24].upper()),
        "seed": int(digest[24:32], 16),
    }


def required_coordinates(
    catalog: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    coordinates: list[dict[str, Any]] = []
    lane_matrix = policy["run_policy"]["lane_matrix"]
    for episode in catalog["episodes"]:
        risk_level = episode["decision_stakes"]["risk_level"]
        for variant_ref, variant, _ in case_variants(episode):
            for lane, requirement in lane_matrix[risk_level].items():
                for paraphrase_index in range(
                    requirement["paraphrases"]
                ):
                    for repeat_index in range(
                        1, requirement["repeats"] + 1
                    ):
                        coordinates.append(
                            coordinate_core(
                                episode_id=episode["episode_id"],
                                case_variant_ref=variant_ref,
                                case_variant=variant,
                                risk_level=risk_level,
                                lane=lane,
                                paraphrase_index=paraphrase_index,
                                repeat_index=repeat_index,
                            )
                        )
    return sorted(coordinates, key=lambda item: item["execution_cell_id"])


def _coordinate_index(
    coordinates: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str, int, int], Mapping[str, Any]]:
    return {
        (
            item["episode_id"],
            item["case_variant_ref"],
            item["lane"],
            item["paraphrase_index"],
            item["repeat_index"],
        ): item
        for item in coordinates
    }


def required_episode_relation_groups(
    catalog: Mapping[str, Any],
    policy: Mapping[str, Any],
    operators: Mapping[str, Mapping[str, Any]],
    coordinates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    coordinate_index = _coordinate_index(coordinates)
    standalone = operators["episode_outcome"]
    paraphrase = operators["meaning_preserving_paraphrase"]
    for coordinate in coordinates:
        groups.append(
            {
                "relation_group_id": "REL-OUTCOME-{}".format(
                    coordinate["execution_cell_id"].removeprefix("CELL-")
                ),
                "operator_ref": standalone["operator_id"],
                "operator_sha256": canonical_sha256(standalone),
                "expected_relation": standalone["expected_relation"],
                "scenario_binding": None,
                "members": [
                    {
                        "execution_cell_id": coordinate[
                            "execution_cell_id"
                        ],
                        "member_role": "singleton",
                    }
                ],
            }
        )
    lane_matrix = policy["run_policy"]["lane_matrix"]
    for episode in catalog["episodes"]:
        episode_id = episode["episode_id"]
        risk_level = episode["decision_stakes"]["risk_level"]
        variants = case_variants(episode)
        for variant_ref, _, _ in variants:
            for lane, requirement in lane_matrix[risk_level].items():
                if requirement["paraphrases"] <= 1:
                    continue
                for repeat_index in range(1, requirement["repeats"] + 1):
                    members = [
                        {
                            "execution_cell_id": coordinate_index[
                                (
                                    episode_id,
                                    variant_ref,
                                    lane,
                                    paraphrase_index,
                                    repeat_index,
                                )
                            ]["execution_cell_id"],
                            "member_role": (
                                "anchor"
                                if paraphrase_index == 0
                                else "subject"
                            ),
                        }
                        for paraphrase_index in range(
                            requirement["paraphrases"]
                        )
                    ]
                    groups.append(
                        {
                            "relation_group_id": "REL-PARA-{}".format(
                                canonical_sha256(members)[:24].upper()
                            ),
                            "operator_ref": paraphrase["operator_id"],
                            "operator_sha256": canonical_sha256(paraphrase),
                            "expected_relation": paraphrase[
                                "expected_relation"
                            ],
                            "scenario_binding": None,
                            "members": members,
                        }
                    )
        siblings_by_operator: dict[str, list[str]] = defaultdict(list)
        for sibling in episode["counterfactual_siblings"]:
            siblings_by_operator[
                AUTOMATIC_OPERATOR_BY_RELATION[sibling["expected_relation"]]
            ].append(sibling["sibling_id"])
        for lane, requirement in lane_matrix[risk_level].items():
            for repeat_index in range(1, requirement["repeats"] + 1):
                anchor = coordinate_index[
                    (
                        episode_id,
                        "base",
                        lane,
                        0,
                        repeat_index,
                    )
                ]
                for operator_ref, sibling_ids in sorted(
                    siblings_by_operator.items()
                ):
                    operator = operators[operator_ref]
                    members = [
                        {
                            "execution_cell_id": anchor[
                                "execution_cell_id"
                            ],
                            "member_role": "anchor",
                        }
                    ] + [
                        {
                            "execution_cell_id": coordinate_index[
                                (
                                    episode_id,
                                    sibling_id,
                                    lane,
                                    0,
                                    repeat_index,
                                )
                            ]["execution_cell_id"],
                            "member_role": "subject",
                        }
                        for sibling_id in sorted(sibling_ids)
                    ]
                    groups.append(
                        {
                            "relation_group_id": "REL-CASE-{}".format(
                                canonical_sha256(
                                    {
                                        "operator_ref": operator_ref,
                                        "members": members,
                                    }
                                )[:24].upper()
                            ),
                            "operator_ref": operator_ref,
                            "operator_sha256": canonical_sha256(operator),
                            "expected_relation": operator[
                                "expected_relation"
                            ],
                            "scenario_binding": None,
                            "members": members,
                        }
                    )
    return sorted(groups, key=lambda item: item["relation_group_id"])


def required_operator_scenario_universe(
    *,
    coordinates: list[dict[str, Any]],
    operators: Mapping[str, Mapping[str, Any]],
    scenarios_by_operator: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    coordinate_index = _coordinate_index(coordinates)
    scenario_coordinates: list[dict[str, Any]] = []
    relation_groups: list[dict[str, Any]] = []
    findings: list[str] = []
    for operator_ref, scenarios in sorted(scenarios_by_operator.items()):
        operator = operators[operator_ref]
        for scenario in sorted(
            scenarios, key=lambda item: item["scenario_id"]
        ):
            anchor = coordinate_index.get(
                (
                    scenario["source_episode_id"],
                    scenario["source_case_variant_ref"],
                    scenario["lane"],
                    0,
                    1,
                )
            )
            if anchor is None:
                findings.append(
                    "{} has no policy-authorized anchor coordinate".format(
                        scenario["scenario_id"]
                    )
                )
                continue
            identity = {
                key: value
                for key, value in anchor.items()
                if key not in {"execution_cell_id", "seed"}
            }
            identity["operator_scenario_ref"] = scenario["scenario_id"]
            digest = canonical_sha256(identity)
            subject = {
                **identity,
                "execution_cell_id": "CELL-{}".format(
                    digest[:24].upper()
                ),
                "seed": int(digest[24:32], 16),
            }
            scenario_coordinates.append(subject)
            relation_groups.append(
                {
                    "relation_group_id": "REL-SCENARIO-{}".format(
                        canonical_sha256(
                            {
                                "scenario_id": scenario["scenario_id"],
                                "anchor": anchor["execution_cell_id"],
                                "subject": subject["execution_cell_id"],
                            }
                        )[:24].upper()
                    ),
                    "operator_ref": operator_ref,
                    "operator_sha256": canonical_sha256(operator),
                    "expected_relation": operator["expected_relation"],
                    "scenario_binding": {
                        "scenario_ref": scenario["scenario_id"],
                        "scenario_sha256": canonical_sha256(scenario),
                        "stimulus_contract_sha256": canonical_sha256(
                            scenario["stimulus_contract"]
                        ),
                    },
                    "members": [
                        {
                            "execution_cell_id": anchor[
                                "execution_cell_id"
                            ],
                            "member_role": "anchor",
                        },
                        {
                            "execution_cell_id": subject[
                                "execution_cell_id"
                            ],
                            "member_role": "subject",
                        },
                    ],
                }
            )
    return (
        sorted(
            scenario_coordinates,
            key=lambda item: item["execution_cell_id"],
        ),
        sorted(relation_groups, key=lambda item: item["relation_group_id"]),
        findings,
    )


def _blocker(code: str, refs: Iterable[str]) -> dict[str, Any] | None:
    unique = sorted(set(refs))
    if not unique:
        return None
    return {"code": code, "count": len(unique), "refs": unique}


def _append_blocker(
    blockers: list[dict[str, Any]],
    code: str,
    refs: Iterable[str],
) -> None:
    blocker = _blocker(code, refs)
    if blocker is not None:
        blockers.append(blocker)


def _variant_message_plans(
    catalog: Mapping[str, Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    return {
        (episode["episode_id"], variant_ref): materialized[
            "user_episode"
        ]["messages"]
        for episode in catalog["episodes"]
        for variant_ref, _, materialized in case_variants(episode)
    }


def validate_paraphrase_authority(
    registry: Mapping[str, Any],
    catalog: Mapping[str, Any],
    required_refs: set[str],
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    findings = schema_findings(registry, PARAPHRASE_SCHEMA_PATH)
    if findings:
        return {}, findings
    variants = _variant_message_plans(catalog)
    entries: dict[str, Mapping[str, Any]] = {}
    for entry in registry["entries"]:
        authority_id = entry["paraphrase_authority_id"]
        if authority_id in entries:
            findings.append(f"duplicate paraphrase authority {authority_id}")
            continue
        entries[authority_id] = entry
        key = (entry["episode_id"], entry["case_variant_ref"])
        source_plan = variants.get(key)
        expected_id = paraphrase_authority_ref(
            entry["episode_id"],
            entry["case_variant_ref"],
            entry["paraphrase_index"],
        )
        if authority_id != expected_id:
            findings.append(f"{authority_id} id differs from canonical slot")
        if source_plan is None:
            findings.append(f"{authority_id} references an unknown case variant")
            continue
        if entry["message_plan_sha256"] != canonical_sha256(
            entry["message_plan"]
        ):
            findings.append(f"{authority_id} message plan hash drifted")
        review = entry["meaning_preservation_review"]
        if review["status"] == "reviewed":
            expected_pair_sha256 = canonical_sha256(
                {
                    "source_message_plan_sha256": canonical_sha256(
                        source_plan
                    ),
                    "candidate_message_plan_sha256": entry[
                        "message_plan_sha256"
                    ],
                }
            )
            if review["source_candidate_pair_sha256"] != expected_pair_sha256:
                findings.append(
                    f"{authority_id} review pair binding drifted"
                )
        source_structure = [
            {key: value for key, value in message.items() if key != "text"}
            for message in source_plan
        ]
        observed_structure = [
            {key: value for key, value in message.items() if key != "text"}
            for message in entry["message_plan"]
        ]
        if observed_structure != source_structure:
            findings.append(f"{authority_id} message structure drifted")
        source_visible = [
            message["text"]
            for message in source_plan
            if message["turn"] <= 1
        ]
        observed_visible = [
            message["text"]
            for message in entry["message_plan"]
            if message["turn"] <= 1
        ]
        if observed_visible == source_visible:
            findings.append(
                f"{authority_id} does not change the evaluated visible wording"
            )
        source_later = [
            message for message in source_plan if message["turn"] > 1
        ]
        observed_later = [
            message
            for message in entry["message_plan"]
            if message["turn"] > 1
        ]
        if observed_later != source_later:
            findings.append(
                f"{authority_id} changes turns outside its visible wording slot"
            )
    unexpected = sorted(set(entries) - required_refs)
    if unexpected:
        findings.append(
            "unexpected paraphrase authorities: {}".format(unexpected)
        )
    return entries, findings


def validate_scenario_authority(
    registry: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    operators: Mapping[str, Mapping[str, Any]],
    automatic_operator_refs: set[str],
    trace_profiles: Mapping[str, Any],
    relation_profiles: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, list[Mapping[str, Any]]], list[str]]:
    findings = schema_findings(registry, SCENARIO_SCHEMA_PATH)
    if findings:
        return {}, findings
    episode_variants = {
        (episode["episode_id"], variant_ref)
        for episode in catalog["episodes"]
        for variant_ref, _, _ in case_variants(episode)
    }
    stages_by_lane = {
        profile["lane"]: set(profile["required_stage_ids"])
        for profile in trace_profiles["profiles"]
    }
    scenarios_by_operator: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    executor_binding = registry["executor_binding"]
    authorized_resolvers = {
        item["resolver_ref"]: item["resolver_release_sha256"]
        for item in executor_binding["authorized_resolvers"]
    }
    if len(authorized_resolvers) != len(
        executor_binding["authorized_resolvers"]
    ):
        findings.append("operator executor contains duplicate resolver refs")
    scenario_ids: set[str] = set()
    for scenario in registry["scenarios"]:
        scenario_id = scenario["scenario_id"]
        if scenario_id in scenario_ids:
            findings.append(f"duplicate operator scenario {scenario_id}")
            continue
        scenario_ids.add(scenario_id)
        operator_ref = scenario["operator_ref"]
        operator = operators.get(operator_ref)
        if operator is None:
            findings.append(f"{scenario_id} references an unknown operator")
            continue
        if operator_ref in automatic_operator_refs:
            findings.append(
                f"{scenario_id} duplicates episode-derived operator authority"
            )
        if scenario["operator_sha256"] != canonical_sha256(operator):
            findings.append(f"{scenario_id} operator hash drifted")
        if scenario["review_status"] == "reviewed":
            reviewed_core = {
                key: value
                for key, value in scenario.items()
                if key not in {"review_status", "review_binding"}
            }
            if scenario["review_binding"][
                "reviewed_scenario_sha256"
            ] != canonical_sha256(reviewed_core):
                findings.append(f"{scenario_id} review binding drifted")
        if (
            scenario["source_episode_id"],
            scenario["source_case_variant_ref"],
        ) not in episode_variants:
            findings.append(f"{scenario_id} references an unknown case variant")
        unknown_stages = set(
            scenario["stimulus_contract"]["target_stage_ids"]
        ) - stages_by_lane[scenario["lane"]]
        if unknown_stages:
            findings.append(
                f"{scenario_id} targets stages outside its lane"
            )
        if executor_binding["status"] == "executable":
            stimulus = scenario["stimulus_contract"]
            if authorized_resolvers.get(
                stimulus["resolver_ref"]
            ) != stimulus["resolver_release_sha256"]:
                findings.append(
                    f"{scenario_id} resolver authority drifted"
                )
            if executor_binding[
                "application_receipt_contract_sha256"
            ] != stimulus["application_receipt_contract_sha256"]:
                findings.append(
                    f"{scenario_id} application receipt contract drifted"
                )
        relation_profile = relation_profiles.get(
            operator["expected_relation"]
        )
        if relation_profile is None or set(
            scenario["required_relation_check_ids"]
        ) != set(relation_profile["required_check_ids"]):
            findings.append(
                f"{scenario_id} relation check authority drifted"
            )
        scenarios_by_operator[operator_ref].append(scenario)
    return dict(scenarios_by_operator), findings


def build_readiness(
    *,
    policy: Mapping[str, Any] | None = None,
    catalog: Mapping[str, Any] | None = None,
    paraphrase_registry: Mapping[str, Any] | None = None,
    operator_registry: Mapping[str, Any] | None = None,
    scenario_registry: Mapping[str, Any] | None = None,
    trace_profiles: Mapping[str, Any] | None = None,
    grader_registry: Mapping[str, Any] | None = None,
    source_run_manifest: Mapping[str, Any] | None = None,
    held_out_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = load_json(POLICY_PATH) if policy is None else policy
    catalog = load_json(CATALOG_PATH) if catalog is None else catalog
    paraphrase_registry = (
        load_json(PARAPHRASE_REGISTRY_PATH)
        if paraphrase_registry is None
        else paraphrase_registry
    )
    operator_registry = (
        load_json(OPERATOR_REGISTRY_PATH)
        if operator_registry is None
        else operator_registry
    )
    scenario_registry = (
        load_json(SCENARIO_REGISTRY_PATH)
        if scenario_registry is None
        else scenario_registry
    )
    trace_profiles = (
        load_json(TRACE_PROFILES_PATH)
        if trace_profiles is None
        else trace_profiles
    )
    grader_registry = (
        load_json(GRADER_REGISTRY_PATH)
        if grader_registry is None
        else grader_registry
    )
    source_run_manifest = (
        load_json(SOURCE_RUN_MANIFEST_PATH)
        if source_run_manifest is None
        else source_run_manifest
    )
    held_out_manifest = (
        load_json(HELD_OUT_MANIFEST_PATH)
        if held_out_manifest is None
        else held_out_manifest
    )
    operators = {
        operator["operator_id"]: operator
        for operator in operator_registry["operators"]
    }
    relation_profiles = {
        profile["expected_relation"]: profile
        for profile in operator_registry["relation_check_profiles"]
    }
    coordinates = required_coordinates(catalog, policy)
    relation_groups = required_episode_relation_groups(
        catalog,
        policy,
        operators,
        coordinates,
    )
    required_paraphrase_refs = {
        coordinate["wording_authority_ref"]
        for coordinate in coordinates
        if coordinate["paraphrase_index"] > 0
    }
    paraphrase_entries, paraphrase_findings = validate_paraphrase_authority(
        paraphrase_registry,
        catalog,
        required_paraphrase_refs,
    )
    automatic_operator_refs = {
        "episode_outcome",
        "meaning_preserving_paraphrase",
        *AUTOMATIC_OPERATOR_BY_RELATION.values(),
    }
    scenarios_by_operator, scenario_findings = validate_scenario_authority(
        scenario_registry,
        catalog=catalog,
        operators=operators,
        automatic_operator_refs=automatic_operator_refs,
        trace_profiles=trace_profiles,
        relation_profiles=relation_profiles,
    )
    (
        scenario_coordinates,
        scenario_relation_groups,
        scenario_universe_findings,
    ) = required_operator_scenario_universe(
        coordinates=coordinates,
        operators=operators,
        scenarios_by_operator=scenarios_by_operator,
    )
    scenario_findings.extend(scenario_universe_findings)
    missing_paraphrases = required_paraphrase_refs - set(
        paraphrase_entries
    )
    required_scenario_operator_refs = {
        operator_ref
        for operator_ref, operator in operators.items()
        if operator["kind"] != "standalone"
        and operator_ref not in automatic_operator_refs
    }
    scenario_coverage_policy = policy["run_policy"][
        "operator_scenario_coverage"
    ]
    minimum_operator_worlds = scenario_coverage_policy[
        "minimum_independent_business_worlds_per_operator"
    ]
    variant_world_keys = {
        (episode["episode_id"], variant_ref): materialized[
            "business_world_independence_key"
        ]
        for episode in catalog["episodes"]
        for variant_ref, _, materialized in case_variants(episode)
    }
    missing_scenario_world_slots: list[str] = []
    missing_scenario_lanes: list[str] = []
    for operator_ref in sorted(required_scenario_operator_refs):
        scenarios = scenarios_by_operator.get(operator_ref, [])
        independent_worlds = {
            variant_world_keys.get(
                (
                    scenario["source_episode_id"],
                    scenario["source_case_variant_ref"],
                )
            )
            for scenario in scenarios
        } - {None}
        missing_scenario_world_slots.extend(
            "{}:independent-world-slot-{}".format(
                operator_ref,
                slot,
            )
            for slot in range(
                len(independent_worlds) + 1,
                minimum_operator_worlds + 1,
            )
        )
        required_lanes = set(
            scenario_coverage_policy["required_lanes_by_operator_kind"][
                operators[operator_ref]["kind"]
            ]
        )
        observed_lanes = {scenario["lane"] for scenario in scenarios}
        missing_scenario_lanes.extend(
            f"{operator_ref}:lane:{lane}"
            for lane in sorted(required_lanes - observed_lanes)
        )
    development_blockers: list[dict[str, Any]] = []
    _append_blocker(
        development_blockers,
        "paraphrase_authority_missing",
        missing_paraphrases,
    )
    _append_blocker(
        development_blockers,
        "paraphrase_authority_invalid",
        paraphrase_findings,
    )
    _append_blocker(
        development_blockers,
        "operator_scenario_authority_missing",
        missing_scenario_world_slots,
    )
    _append_blocker(
        development_blockers,
        "operator_scenario_lane_missing",
        missing_scenario_lanes,
    )
    _append_blocker(
        development_blockers,
        "operator_scenario_authority_invalid",
        scenario_findings,
    )
    if scenario_registry["executor_binding"]["status"] != "executable":
        _append_blocker(
            development_blockers,
            "operator_scenario_executor_unavailable",
            ["operator-scenario-executor"],
        )
    _append_blocker(
        development_blockers,
        "operator_scenario_executor_unverified",
        ["scenario-application-receipt-verifier"],
    )
    if paraphrase_registry["status"] != "reviewed":
        _append_blocker(
            development_blockers,
            "paraphrase_registry_not_reviewed",
            ["paraphrase-authority-registry"],
        )
    _append_blocker(
        development_blockers,
        "paraphrase_entries_not_reviewed",
        (
            authority_id
            for authority_id, entry in paraphrase_entries.items()
            if entry["meaning_preservation_review"]["status"] != "reviewed"
        ),
    )
    if scenario_registry["status"] != "reviewed":
        _append_blocker(
            development_blockers,
            "operator_scenario_registry_not_reviewed",
            ["operator-scenario-authority-registry"],
        )
    _append_blocker(
        development_blockers,
        "operator_scenarios_not_reviewed",
        (
            scenario["scenario_id"]
            for scenarios in scenarios_by_operator.values()
            for scenario in scenarios
            if scenario["review_status"] != "reviewed"
        ),
    )
    formal_blockers = list(development_blockers)
    _append_blocker(
        formal_blockers,
        "formal_execution_admission_unverified",
        ["protected-execution-admission"],
    )
    if source_run_manifest["status"] != "frozen":
        _append_blocker(
            formal_blockers,
            "source_run_manifest_not_frozen",
            ["run-manifest"],
        )
    _append_blocker(
        formal_blockers,
        "model_profiles_not_calibrated",
        (
            profile["profile_id"]
            for profile in grader_registry["evaluator_profiles"]
            if profile["lifecycle_status"] != "calibrated"
        ),
    )
    if held_out_manifest["status"] != "sealed":
        _append_blocker(
            formal_blockers,
            "protected_held_out_manifest_not_sealed",
            ["protected-held-out-manifest"],
        )
    minimum_held_out = policy["held_out_policy"]["minimum_episodes"]
    if len(held_out_manifest["entries"]) < minimum_held_out:
        _append_blocker(
            formal_blockers,
            "protected_held_out_episode_floor_incomplete",
            [
                "missing-held-out-{:02d}".format(index)
                for index in range(
                    len(held_out_manifest["entries"]) + 1,
                    minimum_held_out + 1,
                )
            ],
        )
    operator_coverage = []
    for operator_ref, operator in sorted(operators.items()):
        if operator_ref == "episode_outcome":
            source = "standalone"
            authority_refs = ["catalog:all-execution-coordinates"]
        elif operator_ref in automatic_operator_refs:
            source = "episode_relation"
            authority_refs = sorted(
                sibling["sibling_id"]
                for episode in catalog["episodes"]
                for sibling in episode["counterfactual_siblings"]
                if AUTOMATIC_OPERATOR_BY_RELATION[
                    sibling["expected_relation"]
                ]
                == operator_ref
            )
        elif operator_ref in scenarios_by_operator:
            source = "scenario_registry"
            authority_refs = sorted(
                scenario["scenario_id"]
                for scenario in scenarios_by_operator[operator_ref]
            )
        else:
            source = "missing"
            authority_refs = []
        operator_coverage.append(
            {
                "operator_ref": operator_ref,
                "operator_sha256": canonical_sha256(operator),
                "coverage_source": source,
                "authority_refs": authority_refs,
            }
        )
    risk_variant_counts = Counter(
        episode["decision_stakes"]["risk_level"]
        for episode in catalog["episodes"]
        for _ in case_variants(episode)
    )
    readiness = {
        "artifact_type": "gate3_execution_universe_readiness",
        "artifact_version": "gate3.execution-universe-readiness.v1",
        "authority_hashes": {
            "policy_sha256": canonical_sha256(policy),
            "catalog_sha256": canonical_sha256(catalog),
            "paraphrase_registry_sha256": canonical_sha256(
                paraphrase_registry
            ),
            "operator_registry_sha256": canonical_sha256(operator_registry),
            "operator_scenario_registry_sha256": canonical_sha256(
                scenario_registry
            ),
            "trace_profiles_sha256": canonical_sha256(trace_profiles),
            "grader_registry_sha256": canonical_sha256(grader_registry),
            "source_run_manifest_sha256": canonical_sha256(
                source_run_manifest
            ),
            "protected_held_out_manifest_sha256": canonical_sha256(
                held_out_manifest
            ),
            "compiler_release_sha256": compiler_release_sha256(),
        },
        "universe_summary": {
            "episode_count": len(catalog["episodes"]),
            "case_variant_count": sum(
                len(case_variants(episode))
                for episode in catalog["episodes"]
            ),
            "risk_variant_counts": {
                risk: risk_variant_counts.get(risk, 0)
                for risk in ("medium", "high", "critical")
            },
            "required_coordinate_count": len(coordinates),
            "required_coordinate_set_sha256": canonical_sha256(coordinates),
            "required_episode_relation_group_count": len(relation_groups),
            "required_episode_relation_group_set_sha256": canonical_sha256(
                relation_groups
            ),
            "required_operator_scenario_coordinate_count": len(
                scenario_coordinates
            ),
            "required_operator_scenario_coordinate_set_sha256": (
                canonical_sha256(scenario_coordinates)
            ),
            "required_operator_scenario_relation_group_count": len(
                scenario_relation_groups
            ),
            "required_operator_scenario_relation_group_set_sha256": (
                canonical_sha256(scenario_relation_groups)
            ),
            "required_paraphrase_authority_count": len(
                required_paraphrase_refs
            ),
            "required_operator_scenario_count": (
                len(required_scenario_operator_refs)
                * minimum_operator_worlds
            ),
        },
        "operator_coverage": operator_coverage,
        "development_blockers": sorted(
            development_blockers, key=lambda item: item["code"]
        ),
        "formal_blockers": sorted(
            formal_blockers, key=lambda item: item["code"]
        ),
        "development_status": (
            "blocked" if development_blockers else "ready"
        ),
        "formal_status": "blocked" if formal_blockers else "ready",
    }
    readiness_findings = schema_findings(readiness, READINESS_SCHEMA_PATH)
    if readiness_findings:
        raise ValueError(
            "derived execution universe readiness violates schema:\n- {}".format(
                "\n- ".join(readiness_findings)
            )
        )
    return readiness


def render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--require-development-ready", action="store_true")
    args = parser.parse_args()
    readiness = build_readiness()
    findings: list[str] = []
    if args.write:
        READINESS_PATH.write_text(render(readiness), encoding="utf-8")
    if args.check:
        if not READINESS_PATH.exists():
            findings.append("execution universe readiness is missing")
        elif READINESS_PATH.read_text(encoding="utf-8") != render(readiness):
            findings.append("execution universe readiness is stale")
    if args.require_development_ready and readiness[
        "development_status"
    ] != "ready":
        findings.append("full development execution universe is blocked")
    print(
        json.dumps(
            {
                "status": "failed" if findings else "passed",
                "findings": findings,
                "readiness": readiness,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
