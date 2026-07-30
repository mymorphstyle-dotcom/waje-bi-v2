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
    "source_record_ref",
    "product_grader_profile_ref",
    "authority_profile_ref",
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
    world: Mapping[str, Any],
) -> set[str]:
    """Return facts reachable through the exact AgentWorldView surfaces."""

    return {
        condition["condition_id"]
        for condition in world["data_conditions"]
        if condition["discoverability"]
        in {"provided_to_agent", "discoverable_by_data_probe"}
    } | {
        contract["contract_ref"]
        for contract in world["available_contracts"]
        if contract["discoverability"]
        in {
            "provided_to_agent",
            "discoverable_by_semantic_inspection",
        }
        and contract["state"] == "available"
        and contract["access_state"] == "accessible"
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

    public_context = [
        {
            "ref": condition["condition_id"],
            "description": condition["description"],
        }
        for condition in world["data_conditions"]
        if condition["discoverability"] == "provided_to_agent"
    ] + [
        {
            "ref": contract["contract_ref"],
            "description": contract["description"],
        }
        for contract in world["available_contracts"]
        if contract["discoverability"] == "provided_to_agent"
        and contract["access_state"] == "accessible"
    ]
    accessible_world_refs = agent_accessible_world_refs(world)
    inspection_surfaces = [
        {
            "surface_id": "semantic-contracts",
            "kind": "semantic_contract",
            "discoverable_refs": sorted(
                contract["contract_ref"]
                for contract in world["available_contracts"]
                if contract["contract_ref"] in accessible_world_refs
                and contract["discoverability"]
                == "discoverable_by_semantic_inspection"
            ),
        },
        {
            "surface_id": "data-probes",
            "kind": "data_probe",
            "discoverable_refs": sorted(
                condition["condition_id"]
                for condition in world["data_conditions"]
                if condition["condition_id"] in accessible_world_refs
                and condition["discoverability"]
                == "discoverable_by_data_probe"
            ),
        },
    ]
    agent_world_view: dict[str, Any] = {
        "view_version": "gate3.agent-world-view.v1",
        "evaluation_clock": world["evaluation_clock"],
        "injected_messages": injected_messages,
        "public_context": public_context,
        "inspection_surfaces": inspection_surfaces,
    }
    agent_world_view["view_sha256"] = _hash_view(agent_world_view)

    evaluator_oracle_view: dict[str, Any] = {
        "view_version": "gate3.evaluator-oracle-view.v1",
        "episode_id": episode["episode_id"],
        "episode_core_sha256": corpus_entry["episode_core_sha256"],
        "complete_message_plan": episode["user_episode"]["messages"],
        "truth_facts": world["truth_facts"],
        "decision_stakes": episode["decision_stakes"],
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
