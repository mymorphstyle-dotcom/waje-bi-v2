#!/usr/bin/env python3
"""Compile and validate Gate 3 Agent and evaluator view contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
CATALOG_PATH = EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
CORPUS_REGISTRY_PATH = EVAL_ROOT / "registries" / "corpus-registry.json"
VIEW_SCHEMA_PATH = EVAL_ROOT / "evaluation-views.schema.json"

FORBIDDEN_AGENT_FIELD_NAMES = {
    "title",
    "provenance",
    "decision_stakes",
    "truth_facts",
    "support_expectation",
    "acceptable_outcome",
    "forbidden_outcomes",
    "counterfactual_siblings",
    "coverage_tags",
    "suite_binding",
    "data_source_bindings",
    "source_record_ref",
    "product_grader_profile_ref",
    "authority_profile_ref",
    "authority_expectation",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _hash_view(view: Mapping[str, Any]) -> str:
    value = dict(view)
    value.pop("view_sha256", None)
    return _sha256(value)


def _message_projection(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "turn": message["turn"],
        "speaker": message["speaker"],
        "text": message["text"],
        "communication_function": message.get(
            "communication_function", "question"
        ),
    }


def _contract_observation(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ref": contract["contract_ref"],
        "fact_kind": "contract_status",
        "summary": contract["description"],
        "state": contract["state"],
        "access_state": contract["access_state"],
    }


def _condition_observation(condition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ref": condition["condition_id"],
        "fact_kind": "data_condition",
        "summary": condition["description"],
    }


def _source_observation(binding: Mapping[str, Any]) -> dict[str, Any]:
    materialization_status = binding["materialization_status"]
    if binding["source_mode"] == "known_contract_gap":
        state = "missing"
        summary = (
            "This governed source is an explicit contract gap and cannot "
            "supply quantified evidence."
        )
    elif materialization_status == "verified":
        state = "available"
        summary = (
            "This governed evaluation source has a verified materialization "
            "on this inspection surface."
        )
    else:
        state = "missing"
        summary = (
            "This governed evaluation source is planned and has no admitted "
            "materialization yet."
        )
    return {
        "ref": binding["source_ref"],
        "fact_kind": "source_binding",
        "summary": summary,
        "state": state,
        "source_mode": binding["source_mode"],
        "materialization_status": materialization_status,
    }


def _sorted_observations(
    observations: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return sorted(observations, key=lambda item: item["ref"])


def _find_forbidden_field_names(value: Any) -> set[str]:
    findings: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in FORBIDDEN_AGENT_FIELD_NAMES:
                findings.add(key)
            findings.update(_find_forbidden_field_names(child))
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for child in value:
            findings.update(_find_forbidden_field_names(child))
    return findings


def agent_accessible_world_refs(
    episode_or_world: Mapping[str, Any],
    *,
    visible_turn: int | None = None,
) -> set[str]:
    """Return refs inspectable through the exact AgentWorldView surfaces.

    A planned or missing source binding is inspectable as an availability fact.
    Presence here never authorizes it as materialized evidence.
    """

    if "business_world" in episode_or_world:
        episode = episode_or_world
        world = episode["business_world"]
        source_bindings = episode.get("data_source_bindings", [])
    else:
        world = episode_or_world
        source_bindings = []
    released_contract_refs = {
        event["affected_ref"]
        for event in world.get("scheduled_events", [])
        if event["event_type"] == "contract_release"
        and event.get("affected_ref")
        and (
            visible_turn is None
            or event["after_user_turn"] <= visible_turn
        )
    }
    future_release_refs = {
        event["affected_ref"]
        for event in world.get("scheduled_events", [])
        if event["event_type"] == "contract_release"
        and event.get("affected_ref")
        and visible_turn is not None
        and event["after_user_turn"] > visible_turn
    }
    return {
        condition["condition_id"]
        for condition in world["data_conditions"]
        if condition["discoverability"]
        in {
            "provided_to_agent",
            "discoverable_by_semantic_inspection",
            "discoverable_by_data_probe",
        }
        and condition["condition_id"] not in future_release_refs
    } | {
        contract["contract_ref"]
        for contract in world["available_contracts"]
        if contract["discoverability"]
        in {
            "provided_to_agent",
            "discoverable_by_semantic_inspection",
        }
        and contract["contract_ref"] not in future_release_refs
    } | released_contract_refs | {
        binding["source_ref"]
        for binding in source_bindings
        if (
            visible_turn is None
            or binding["agent_access"]["available_from_turn"] <= visible_turn
        )
    }


def _future_release_refs(
    world: Mapping[str, Any], *, visible_turn: int
) -> set[str]:
    return {
        event["affected_ref"]
        for event in world.get("scheduled_events", [])
        if event["event_type"] == "contract_release"
        and event.get("affected_ref")
        and event["after_user_turn"] > visible_turn
    }


def _binding_visible(
    binding: Mapping[str, Any], *, visible_turn: int
) -> bool:
    return binding["agent_access"]["available_from_turn"] <= visible_turn


def agent_materialized_source_refs(
    episode: Mapping[str, Any],
    *,
    visible_turn: int | None = None,
) -> set[str]:
    """Return source refs whose evidence artifacts are admitted and visible."""

    return {
        binding["source_ref"]
        for binding in episode.get("data_source_bindings", [])
        if binding["materialization_status"] == "verified"
        and (
            visible_turn is None
            or binding["agent_access"]["available_from_turn"] <= visible_turn
        )
    }


def compile_views(
    episode: Mapping[str, Any],
    corpus_entry: Mapping[str, Any],
    *,
    visible_turn: int = 1,
) -> dict[str, Any]:
    world = episode["business_world"]
    injected_messages = [
        _message_projection(message)
        for message in episode["user_episode"]["messages"]
        if message["turn"] <= visible_turn
    ]
    if not injected_messages:
        raise ValueError("AgentWorldView requires at least one injected message")

    visible_events = [
        event
        for event in world.get("scheduled_events", [])
        if event["after_user_turn"] <= visible_turn
    ]
    future_release_refs = _future_release_refs(
        world, visible_turn=visible_turn
    )
    public_context = [
        {
            "ref": condition["condition_id"],
            "description": condition["description"],
        }
        for condition in world["data_conditions"]
        if condition["discoverability"] == "provided_to_agent"
        and condition["condition_id"] not in future_release_refs
    ] + [
        {
            "ref": contract["contract_ref"],
            "description": contract["description"],
        }
        for contract in world["available_contracts"]
        if contract["discoverability"] == "provided_to_agent"
        and contract["access_state"] == "accessible"
        and contract["contract_ref"] not in future_release_refs
    ]
    semantic_contracts = [
        contract
        for contract in world["available_contracts"]
        if contract["contract_ref"] not in future_release_refs
        and contract["discoverability"]
        == "discoverable_by_semantic_inspection"
    ]
    semantic_conditions = [
        condition
        for condition in world["data_conditions"]
        if condition["condition_id"] not in future_release_refs
        and condition["discoverability"]
        == "discoverable_by_semantic_inspection"
    ]
    semantic_bindings = [
        binding
        for binding in episode["data_source_bindings"]
        if _binding_visible(binding, visible_turn=visible_turn)
        and binding["agent_access"]["surface"] == "semantic_inspection"
    ]
    probe_conditions = [
        condition
        for condition in world["data_conditions"]
        if condition["condition_id"] not in future_release_refs
        and condition["discoverability"]
        == "discoverable_by_data_probe"
    ]
    probe_bindings = [
        binding
        for binding in episode["data_source_bindings"]
        if _binding_visible(binding, visible_turn=visible_turn)
        and binding["agent_access"]["surface"] == "data_probe"
    ]
    authority_bindings = [
        binding
        for binding in episode["data_source_bindings"]
        if _binding_visible(binding, visible_turn=visible_turn)
        and binding["agent_access"]["surface"] == "authority_lookup"
    ]
    inspection_surfaces = [
        {
            "surface_id": "semantic-contracts",
            "kind": "semantic_contract",
            "discoverable_refs": sorted(
                {
                    contract["contract_ref"]
                    for contract in semantic_contracts
                }
                | {
                    condition["condition_id"]
                    for condition in semantic_conditions
                }
                | {
                    binding["source_ref"]
                    for binding in semantic_bindings
                }
            ),
            "observations": _sorted_observations(
                [
                    *(
                        _contract_observation(contract)
                        for contract in semantic_contracts
                    ),
                    *(
                        _condition_observation(condition)
                        for condition in semantic_conditions
                    ),
                    *(
                        _source_observation(binding)
                        for binding in semantic_bindings
                    ),
                ]
            ),
        },
        {
            "surface_id": "data-probes",
            "kind": "data_probe",
            "discoverable_refs": sorted(
                {
                    condition["condition_id"]
                    for condition in probe_conditions
                }
                | {
                    binding["source_ref"]
                    for binding in probe_bindings
                }
            ),
            "observations": _sorted_observations(
                [
                    *(
                        _condition_observation(condition)
                        for condition in probe_conditions
                    ),
                    *(
                        _source_observation(binding)
                        for binding in probe_bindings
                    ),
                ]
            ),
        },
        {
            "surface_id": "authority-records",
            "kind": "authority_lookup",
            "discoverable_refs": sorted(
                binding["source_ref"]
                for binding in authority_bindings
            ),
            "observations": _sorted_observations(
                [
                    _source_observation(binding)
                    for binding in authority_bindings
                ]
            ),
        },
    ]
    agent_world_view: dict[str, Any] = {
        "view_version": "gate3.agent-world-view.v2",
        "evaluation_clock": world["evaluation_clock"],
        "injected_messages": injected_messages,
        "injected_events": [
            {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "public_payload": event["public_payload"],
                **(
                    {"affected_ref": event["affected_ref"]}
                    if event.get("affected_ref")
                    else {}
                ),
            }
            for event in visible_events
        ],
        "public_context": public_context,
        "inspection_surfaces": inspection_surfaces,
    }
    agent_world_view["view_sha256"] = _hash_view(agent_world_view)

    evaluator_oracle_view: dict[str, Any] = {
        "view_version": "gate3.evaluator-oracle-view.v2",
        "episode_id": episode["episode_id"],
        "episode_core_sha256": corpus_entry["episode_core_sha256"],
        "complete_message_plan": episode["user_episode"]["messages"],
        "truth_facts": world["truth_facts"],
        "scheduled_events": world.get("scheduled_events", []),
        "decision_stakes": episode["decision_stakes"],
        "suite_binding": episode["suite_binding"],
        "data_source_bindings": episode["data_source_bindings"],
        "support_expectation": episode["support_expectation"],
        "acceptable_outcome": episode["acceptable_outcome"],
        "forbidden_outcomes": episode["forbidden_outcomes"],
        "counterfactual_siblings": episode["counterfactual_siblings"],
        "source_record_ref": corpus_entry["source_record_ref"],
        "product_grader_profile_ref": corpus_entry[
            "product_grader_profile_ref"
        ],
        "authority_profile_ref": corpus_entry["authority_profile_ref"],
    }
    evaluator_oracle_view["view_sha256"] = _hash_view(
        evaluator_oracle_view
    )
    bundle = {
        "agent_world_view": agent_world_view,
        "evaluator_oracle_view": evaluator_oracle_view,
    }
    forbidden_fields = _find_forbidden_field_names(agent_world_view)
    if forbidden_fields:
        raise ValueError(
            "AgentWorldView contains forbidden fields: {}".format(
                ", ".join(sorted(forbidden_fields))
            )
        )
    return bundle


def validate_all_views() -> list[str]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    corpus = json.loads(CORPUS_REGISTRY_PATH.read_text(encoding="utf-8"))
    schema = json.loads(VIEW_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    entries = {
        entry["episode_id"]: entry for entry in corpus["entries"]
    }
    findings: list[str] = []
    for episode in catalog["episodes"]:
        bundle = compile_views(episode, entries[episode["episode_id"]])
        for error in validator.iter_errors(bundle):
            location = "/".join(str(part) for part in error.absolute_path)
            findings.append(
                "{} {}: {}".format(
                    episode["episode_id"], location or "<root>", error.message
                )
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    arguments = parser.parse_args()

    findings = validate_all_views()
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
