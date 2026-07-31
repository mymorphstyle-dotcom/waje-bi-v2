#!/usr/bin/env python3
"""Validate the Gate 3 authority observation package."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(relative_path: str, *, decimal_numbers: bool = False) -> dict[str, Any]:
    options: dict[str, Any] = {"object_pairs_hook": _reject_duplicate_keys}
    if decimal_numbers:
        options.update(parse_int=Decimal, parse_float=Decimal)
    with (ROOT / relative_path).open(encoding="utf-8") as handle:
        value = json.load(handle, **options)
    if not isinstance(value, dict):
        raise ValueError(f"{relative_path} must contain a JSON object")
    return value


def _validate_schemas_and_instances(
    registry: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for group in ("schema_catalog", "schedules", "fixtures", "examples"):
        for entry in registry[group]:
            path = ROOT / entry["path"]
            if not path.is_file():
                raise ValueError(f"registered path does not exist: {path}")
            loaded[entry["path"]] = _load(entry["path"])

    schemas = {
        entry["schema_id"]: loaded[entry["path"]]
        for entry in registry["schema_catalog"]
    }
    for entry in registry["schema_catalog"]:
        schema = loaded[entry["path"]]
        Draft202012Validator.check_schema(schema)
        if schema["$id"] != entry["schema_id"]:
            raise ValueError(f"schema id mismatch: {entry['path']}")

    registry_schema_id = next(
        entry["schema_id"]
        for entry in registry["schema_catalog"]
        if entry["artifact_kind"] == "authority_registry"
    )
    Draft202012Validator(
        schemas[registry_schema_id],
        format_checker=FormatChecker(),
    ).validate(registry)

    for group in ("schedules", "fixtures", "examples"):
        for entry in registry[group]:
            Draft202012Validator(
                schemas[entry["schema_id"]],
                format_checker=FormatChecker(),
            ).validate(loaded[entry["path"]])
    return loaded


def _validate_registry_binding(
    registry: dict[str, Any],
    schedule: dict[str, Any],
    fixture: dict[str, Any],
    bundle: dict[str, Any],
) -> None:
    binding = registry["episode_bindings"][0]
    expected = (
        fixture["episode_id"],
        fixture["source_authority_id"],
        fixture["fixture_id"],
        schedule["schedule_id"],
        bundle["bundle_id"],
    )
    actual = (
        binding["episode_id"],
        binding["source_authority_id"],
        binding["fixture_id"],
        binding["schedule_id"],
        binding["example_bundle_id"],
    )
    if actual != expected:
        raise ValueError("episode binding does not resolve to registered artifacts")
    if bundle["fixture_ref"] != fixture["fixture_uri"]:
        raise ValueError("bundle fixture_ref does not resolve")
    if bundle["accepted_authority"] != fixture["accepted_authority"]:
        raise ValueError("bundle and fixture accepted authority differ")


def _validate_schedule(schedule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    milestones = {
        item["milestone_id"]: item for item in schedule["milestones"]
    }
    if len(milestones) != len(schedule["milestones"]):
        raise ValueError("duplicate milestone id")
    ordinals = [item["ordinal"] for item in schedule["milestones"]]
    if ordinals != list(range(1, len(milestones) + 1)):
        raise ValueError("milestone ordinals must be contiguous and ordered")
    for item in schedule["milestones"]:
        for predecessor in item["predecessor_milestone_ids"]:
            if predecessor not in milestones:
                raise ValueError(f"unknown predecessor milestone: {predecessor}")
            if milestones[predecessor]["ordinal"] >= item["ordinal"]:
                raise ValueError(f"non-prior predecessor milestone: {predecessor}")
    for scenario in schedule["scenarios"]:
        unknown = set(scenario["required_milestone_ids"]) - set(milestones)
        if unknown:
            raise ValueError(f"scenario references unknown milestones: {unknown}")
    return milestones


def _declared_fixture_refs(fixture: dict[str, Any]) -> set[str]:
    declared = set(fixture["accepted_authority"].values())
    declared.update(fixture["journal_object_refs"])
    for item in fixture["object_inventory"]:
        declared.add(item["object_ref"])
        declared.update(item["authority_refs"])
    for event in fixture["source_events"]:
        declared.add(event["source_event_ref"])
        declared.add(event["run_attempt_ref"])
        if "related_event_ref" in event:
            declared.add(event["related_event_ref"])
    return declared


def _validate_observations(
    bundle: dict[str, Any],
    milestones: dict[str, dict[str, Any]],
    declared_refs: set[str],
    allowed_claim_refs: set[str],
) -> None:
    observations = bundle["observations"]
    by_id = {item["observation_id"]: item for item in observations}
    if len(by_id) != len(observations):
        raise ValueError("duplicate observation id")
    if [item["sequence"] for item in observations] != list(
        range(1, len(observations) + 1)
    ):
        raise ValueError("observation sequences must be contiguous and ordered")
    if len({item["milestone_id"] for item in observations}) != len(observations):
        raise ValueError("example must observe each scheduled milestone once")

    seen: set[str] = set()
    appended_refs: set[str] = set()
    for item in observations:
        milestone = milestones[item["milestone_id"]]
        if item["kind"] != milestone["kind"]:
            raise ValueError(f"milestone kind mismatch: {item['observation_id']}")
        if (
            item["authority_effect"]["effect_kind"]
            != milestone["expected_effect_kind"]
        ):
            raise ValueError(f"effect kind mismatch: {item['observation_id']}")
        if not set(item["causation_observation_ids"]).issubset(seen):
            raise ValueError(f"future causation reference: {item['observation_id']}")
        if item["event_identity"]["source_event_ref"] not in declared_refs:
            raise ValueError(
                "unresolved event source ref in "
                f"{item['observation_id']}: "
                f"{item['event_identity']['source_event_ref']}"
            )
        unresolved = set(item["authority_refs"]) - declared_refs
        if unresolved:
            raise ValueError(
                f"unresolved authority refs in {item['observation_id']}: "
                f"{sorted(unresolved)}"
            )
        unknown_claims = set(item["claim_refs"]) - allowed_claim_refs
        if unknown_claims:
            raise ValueError(
                f"unknown claim refs in {item['observation_id']}: "
                f"{sorted(unknown_claims)}"
            )
        if (
            _canonical_sha256(item["event_identity"]["payload"])
            != item["event_identity"]["payload_sha256"]
        ):
            raise ValueError(
                f"payload digest mismatch: {item['observation_id']}"
            )
        effect = item["authority_effect"]
        relation_fields = (
            "appended_object_refs",
            "invalidated_object_refs",
            "superseded_object_refs",
        )
        effect_refs = {
            field: set(effect[field]) for field in relation_fields
        }
        unresolved_effect_refs = set().union(*effect_refs.values()) - declared_refs
        if unresolved_effect_refs:
            raise ValueError(
                f"unresolved effect refs in {item['observation_id']}: "
                f"{sorted(unresolved_effect_refs)}"
            )
        if (
            effect_refs["appended_object_refs"]
            & effect_refs["invalidated_object_refs"]
            or effect_refs["appended_object_refs"]
            & effect_refs["superseded_object_refs"]
            or effect_refs["invalidated_object_refs"]
            & effect_refs["superseded_object_refs"]
        ):
            raise ValueError(
                f"contradictory effect refs: {item['observation_id']}"
            )
        repeated_appends = (
            effect_refs["appended_object_refs"] & appended_refs
        )
        if repeated_appends:
            raise ValueError(
                f"authority object appended twice in {item['observation_id']}: "
                f"{sorted(repeated_appends)}"
            )
        if item["kind"] == "duplicate_rejected":
            original = by_id[item["details"]["duplicate_of_observation_id"]]
            if original["sequence"] >= item["sequence"]:
                raise ValueError("duplicate points to a non-prior observation")
            if item["event_identity"]["idempotency_key"] != original[
                "event_identity"
            ]["idempotency_key"]:
                raise ValueError("duplicate idempotency key differs")
            if item["event_identity"]["payload_sha256"] != original[
                "event_identity"
            ]["payload_sha256"]:
                raise ValueError("duplicate payload digest differs")
            if any(item["authority_effect"][field] for field in relation_fields):
                raise ValueError("duplicate observation changes authority")
        appended_refs.update(effect_refs["appended_object_refs"])
        seen.add(item["observation_id"])

    observed_times = [
        datetime.fromisoformat(item["observed_at"]) for item in observations
    ]
    if observed_times != sorted(observed_times):
        raise ValueError("observation times are not monotonic")


def _validate_fixture(fixture: dict[str, Any]) -> None:
    objects = {
        item["object_ref"]: item for item in fixture["object_inventory"]
    }
    if len(objects) != len(fixture["object_inventory"]):
        raise ValueError("duplicate fixture object ref")
    for ref in fixture["accepted_authority"].values():
        if not ref.startswith(("case:", "cycle:")) and ref not in objects:
            raise ValueError(f"accepted authority ref missing from fixture: {ref}")
    for item in fixture["object_inventory"]:
        prior_ref = item["prior_object_ref"]
        if prior_ref is None:
            continue
        if prior_ref not in objects:
            raise ValueError(f"prior object ref missing: {prior_ref}")
        if objects[prior_ref]["version"] >= item["version"]:
            raise ValueError(f"non-increasing object version: {item['object_ref']}")
    for ref in fixture["expected_append_only_repair"]["appended_object_refs"]:
        if ref not in objects:
            raise ValueError(f"expected appended object missing: {ref}")

    facts = _load(
        "fixtures/g3-user-008-prior-authority.v1.json",
        decimal_numbers=True,
    )["business_facts"]
    if (
        facts["prior_target_paid_amount"] - facts["duplicate_paid_amount"]
        != facts["repaired_target_paid_amount"]
    ):
        raise ValueError("fixture repaired amount does not reconcile")
    prior_growth = Decimal(100) * (
        facts["prior_target_paid_amount"] / facts["baseline_paid_amount"] - 1
    )
    repaired_growth = Decimal(100) * (
        facts["repaired_target_paid_amount"] / facts["baseline_paid_amount"] - 1
    )
    if prior_growth != facts["prior_growth_pct"]:
        raise ValueError("fixture prior growth does not reconcile")
    if repaired_growth != facts["repaired_growth_pct"]:
        raise ValueError("fixture repaired growth does not reconcile")


def _validate_claim_dispositions(
    fixture: dict[str, Any],
    bundle: dict[str, Any],
    source_authority: dict[str, Any],
    declared_refs: set[str],
) -> None:
    expected_items = fixture["expected_append_only_repair"][
        "expected_claim_dispositions"
    ]
    expected = {item["claim_ref"]: item for item in expected_items}
    if len(expected) != len(expected_items):
        raise ValueError("duplicate expected claim ref")
    actual_items = bundle["claim_dispositions"]
    actual = {item["claim_ref"]: item for item in actual_items}
    if len(actual) != len(actual_items):
        raise ValueError("duplicate bundle claim ref")
    disposition_refs = {
        item["disposition_ref"] for item in actual_items
    }
    if len(disposition_refs) != len(actual_items):
        raise ValueError("duplicate disposition ref")
    if set(actual) != set(expected):
        raise ValueError("bundle claim dispositions differ from fixture")
    for claim_ref, expected_item in expected.items():
        actual_item = actual[claim_ref]
        for field in ("claim_target_ids", "disposition", "reason"):
            if actual_item[field] != expected_item[field]:
                raise ValueError(
                    f"claim disposition {field} differs for {claim_ref}"
                )

    repair = fixture["expected_append_only_repair"]
    projection = bundle["current_projection"]
    if projection["frame_revision_ref"] != fixture["accepted_authority"][
        "frame_revision_ref"
    ]:
        raise ValueError("current frame projection differs from fixture")
    if projection["plan_revision_ref"] != fixture["accepted_authority"][
        "plan_revision_ref"
    ]:
        raise ValueError("current plan projection differs from fixture")
    if projection["answer_version_ref"] != repair["current_answer_version_ref"]:
        raise ValueError("current answer projection differs from fixture")
    if (
        projection["workflow_projection_ref"]
        != repair["current_workflow_projection_ref"]
    ):
        raise ValueError("current workflow projection differs from fixture")

    objects = {
        item["object_ref"]: item for item in fixture["object_inventory"]
    }
    current_answer = objects.get(projection["answer_version_ref"])
    current_workflow = objects.get(projection["workflow_projection_ref"])
    if current_answer is None or current_answer["object_type"] != "answer_version":
        raise ValueError("current answer object is unresolved")
    if (
        current_workflow is None
        or current_workflow["object_type"] != "workflow_projection"
    ):
        raise ValueError("current workflow object is unresolved")
    invalidated_refs = set(repair["invalidated_object_refs"])
    if invalidated_refs & set(current_answer["authority_refs"]):
        raise ValueError("current answer cites invalidated evidence")

    episode_catalog = _load(
        "../candidates/wajegame_launch_question_episodes.json"
    )
    episode = next(
        item
        for item in episode_catalog["episodes"]
        if item["episode_id"] == fixture["episode_id"]
    )
    episode_claim_target_ids = {
        item["claim_target_id"]
        for item in episode["acceptable_outcome"]["claim_targets"]
    }
    authority_claim_target_ids = set(
        source_authority["scope_claim_target_ids"]
    )
    appended_refs = {
        ref
        for observation in bundle["observations"]
        for ref in observation["authority_effect"]["appended_object_refs"]
    }
    superseded_refs = {
        ref
        for observation in bundle["observations"]
        for ref in observation["authority_effect"]["superseded_object_refs"]
    }
    expected_current_answer = projection["answer_version_ref"]
    for item in actual_items:
        if item["disposition_ref"] not in declared_refs:
            raise ValueError(
                f"unresolved disposition ref: {item['disposition_ref']}"
            )
        if item["disposition_ref"] not in appended_refs:
            raise ValueError(
                f"disposition was not appended: {item['disposition_ref']}"
            )
        previous_ref = item.get("previous_disposition_ref")
        if previous_ref is not None:
            if previous_ref not in declared_refs:
                raise ValueError(
                    f"unresolved previous disposition ref: {previous_ref}"
                )
            if previous_ref not in superseded_refs:
                raise ValueError(
                    f"previous disposition was not superseded: {previous_ref}"
                )
        if item["answer_version_ref"] != expected_current_answer:
            raise ValueError(
                f"claim points at stale answer: {item['claim_ref']}"
            )
        target_ids = set(item["claim_target_ids"])
        if not target_ids.issubset(episode_claim_target_ids):
            raise ValueError(
                f"claim target is outside Episode: {item['claim_ref']}"
            )
        if not target_ids.issubset(authority_claim_target_ids):
            raise ValueError(
                f"claim target is outside source authority: {item['claim_ref']}"
            )
        for evidence_ref in item["evidence_record_refs"]:
            evidence = objects.get(evidence_ref)
            if evidence is None or evidence["object_type"] != "evidence_record":
                raise ValueError(
                    f"claim evidence ref is unresolved: {evidence_ref}"
                )
            if evidence_ref in invalidated_refs:
                raise ValueError(
                    f"claim cites invalidated evidence: {item['claim_ref']}"
                )
            if evidence_ref not in current_answer["authority_refs"]:
                raise ValueError(
                    f"claim evidence is outside current answer: "
                    f"{item['claim_ref']}"
                )

    active_claim_refs = {
        item["claim_ref"]
        for item in actual_items
        if item["disposition"] not in {"revoked", "unverifiable"}
    }
    withheld_claim_refs = set(actual) - active_claim_refs
    if set(projection["active_claim_refs"]) != active_claim_refs:
        raise ValueError("active claim projection differs from dispositions")
    if set(projection["withheld_claim_refs"]) != withheld_claim_refs:
        raise ValueError("withheld claim projection differs from dispositions")


def _validate_source_authority(fixture: dict[str, Any]) -> dict[str, Any]:
    case_registry = _load("../case-files/case-file-authorities.json")
    matches = [
        item
        for item in case_registry["authorities"]
        if item["authority_id"] == fixture["source_authority_id"]
    ]
    if len(matches) != 1:
        raise ValueError("fixture source authority is not registered")
    return matches[0]


def _validate_negative_gate_cases(
    schema: dict[str, Any],
    bundle: dict[str, Any],
    fixture: dict[str, Any],
    milestones: dict[str, dict[str, Any]],
    source_authority: dict[str, Any],
) -> int:
    validator = Draft202012Validator(schema)
    mutations: list[dict[str, Any]] = []
    candidate = copy.deepcopy(bundle)
    candidate["gate_contract"]["authority_state"] = "settled"
    mutations.append(candidate)
    candidate = copy.deepcopy(bundle)
    candidate["current_projection"]["answer_status"] = "settled"
    mutations.append(candidate)
    candidate = copy.deepcopy(bundle)
    candidate["current_projection"]["delivery_state"] = "delivered"
    mutations.append(candidate)
    candidate = copy.deepcopy(bundle)
    candidate["claim_dispositions"][0]["disposition"] = "settled"
    mutations.append(candidate)
    candidate = copy.deepcopy(bundle)
    duplicate = next(
        item
        for item in candidate["observations"]
        if item["kind"] == "duplicate_rejected"
    )
    duplicate["authority_effect"]["appended_object_refs"] = ["evidence:illegal"]
    mutations.append(candidate)
    candidate = copy.deepcopy(bundle)
    candidate["episode_disposition"] = "supported_provisional"
    mutations.append(candidate)
    if any(not list(validator.iter_errors(item)) for item in mutations):
        raise ValueError("a forbidden Gate 3 mutation passed schema validation")

    semantic_mutations: list[tuple[str, dict[str, Any]]] = []
    candidate = copy.deepcopy(bundle)
    candidate["observations"][0]["event_identity"]["payload_sha256"] = "0" * 64
    semantic_mutations.append(("payload digest", candidate))
    candidate = copy.deepcopy(bundle)
    candidate["observations"][0]["authority_effect"][
        "appended_object_refs"
    ] = ["evidence:ghost"]
    semantic_mutations.append(("ghost object", candidate))
    candidate = copy.deepcopy(bundle)
    candidate["observations"][0]["claim_refs"] = ["claim:invented"]
    semantic_mutations.append(("invented claim", candidate))
    candidate = copy.deepcopy(bundle)
    duplicate_disposition = copy.deepcopy(candidate["claim_dispositions"][0])
    duplicate_disposition["disposition_ref"] = "disposition:conflicting"
    duplicate_disposition["disposition"] = "revoked"
    candidate["claim_dispositions"].append(duplicate_disposition)
    semantic_mutations.append(("duplicate claim disposition", candidate))
    candidate = copy.deepcopy(bundle)
    candidate["claim_dispositions"][0]["evidence_record_refs"] = [
        fixture["expected_append_only_repair"]["invalidated_object_refs"][0]
    ]
    semantic_mutations.append(("invalidated evidence", candidate))

    declared_refs = _declared_fixture_refs(fixture)
    allowed_claim_refs = {
        item["claim_ref"]
        for item in fixture["expected_append_only_repair"][
            "expected_claim_dispositions"
        ]
    }
    for label, candidate in semantic_mutations:
        try:
            _validate_observations(
                candidate,
                milestones,
                declared_refs,
                allowed_claim_refs,
            )
            _validate_claim_dispositions(
                fixture,
                candidate,
                source_authority,
                declared_refs,
            )
        except ValueError:
            continue
        raise ValueError(f"forbidden semantic mutation passed: {label}")
    return len(mutations) + len(semantic_mutations)


def validate_package() -> dict[str, int]:
    registry = _load("authority-registry.json")
    loaded = _validate_schemas_and_instances(registry)
    schedule = loaded[registry["schedules"][0]["path"]]
    fixture = loaded[registry["fixtures"][0]["path"]]
    bundle = loaded[registry["examples"][0]["path"]]
    _validate_registry_binding(registry, schedule, fixture, bundle)
    milestones = _validate_schedule(schedule)
    _validate_fixture(fixture)
    source_authority = _validate_source_authority(fixture)
    declared_refs = _declared_fixture_refs(fixture)
    allowed_claim_refs = {
        item["claim_ref"]
        for item in fixture["expected_append_only_repair"][
            "expected_claim_dispositions"
        ]
    }
    _validate_observations(
        bundle,
        milestones,
        declared_refs,
        allowed_claim_refs,
    )
    _validate_claim_dispositions(
        fixture,
        bundle,
        source_authority,
        declared_refs,
    )
    negative_cases = _validate_negative_gate_cases(
        loaded["authority-observation-bundle.schema.json"],
        bundle,
        fixture,
        milestones,
        source_authority,
    )
    return {
        "schemas": len(registry["schema_catalog"]),
        "milestones": len(milestones),
        "observations": len(bundle["observations"]),
        "claim_dispositions": len(bundle["claim_dispositions"]),
        "negative_cases": negative_cases,
    }


def main() -> None:
    summary = validate_package()
    print(
        "authority-package-ok "
        f"schemas={summary['schemas']} "
        f"milestones={summary['milestones']} "
        f"observations={summary['observations']} "
        f"claim_dispositions={summary['claim_dispositions']} "
        f"negative_cases={summary['negative_cases']}"
    )


if __name__ == "__main__":
    main()
