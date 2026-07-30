#!/usr/bin/env python3
"""Validate Gate 3 behavior-first evaluation catalogs and report coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
SCHEMA_PATH = EVAL_ROOT / "evaluation-episode.schema.json"
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
RUBRIC_PATH = EVAL_ROOT / "grader-rubric.json"
READINESS_PATH = EVAL_ROOT / "gate3-e0-readiness.json"
AUTHORING_CATALOG_PATH = (
    EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _format_error(error: Any) -> str:
    location = "/".join(str(part) for part in error.absolute_path)
    return "{}: {}".format(location or "<root>", error.message)


def _flatten_tags(
    episodes: Sequence[Mapping[str, Any]], dimension: str
) -> set[str]:
    return {
        value
        for episode in episodes
        for value in episode["coverage_tags"][dimension]
    }


def _duplicates(values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reviewable_episode_hash(episode: Mapping[str, Any]) -> str:
    reviewable = json.loads(json.dumps(episode, ensure_ascii=False))
    reviewable["provenance"].pop("review_status", None)
    reviewable["provenance"].pop("review_attestations", None)
    payload = json.dumps(
        reviewable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _coverage_report(
    episodes: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    *,
    catalog_path: Path,
) -> dict[str, Any]:
    source_counts = Counter(episode["source_pool"] for episode in episodes)
    review_counts = Counter(
        episode["provenance"]["review_status"] for episode in episodes
    )
    coverage: dict[str, Any] = {}
    missing_coverage: dict[str, list[str]] = {}
    for dimension, required_values in policy["required_coverage"].items():
        observed = _flatten_tags(episodes, dimension)
        coverage[dimension] = sorted(observed)
        missing = sorted(set(required_values) - observed)
        if missing:
            missing_coverage[dimension] = missing

    source_gaps = {
        source_pool: minimum - source_counts[source_pool]
        for source_pool, minimum in policy["minimum_catalog"][
            "source_pool_minimums"
        ].items()
        if source_counts[source_pool] < minimum
    }
    reviewed_count = sum(
        episode["provenance"]["review_status"] == "fully_reviewed"
        and episode["dataset_partition"] != "authoring"
        for episode in episodes
    )
    reviewed_gap = max(
        0,
        policy["minimum_catalog"]["reviewed_base_episodes"] - reviewed_count,
    )
    multi_turn_count = sum(
        len(episode["user_episode"]["messages"]) > 1 for episode in episodes
    )
    multi_turn_gap = max(
        0,
        policy["minimum_catalog"]["multi_turn_episodes"] - multi_turn_count,
    )
    critical_risk_count = sum(
        episode["decision_stakes"]["risk_level"] in {"high", "critical"}
        for episode in episodes
    )
    critical_risk_gap = max(
        0,
        policy["minimum_catalog"]["critical_risk_episodes"]
        - critical_risk_count,
    )
    required_roles = set(
        policy["minimum_catalog"]["required_counterfactual_roles_per_episode"]
    )
    counterfactual_role_gaps = {
        episode["episode_id"]: sorted(
            required_roles
            - {
                sibling["expected_relation"]
                if sibling["expected_relation"]
                not in {"boundary_changing", "interaction_changing"}
                else "boundary_changing_or_interaction_changing"
                for sibling in episode["counterfactual_siblings"]
            }
        )
        for episode in episodes
    }
    counterfactual_role_gaps = {
        episode_id: gaps
        for episode_id, gaps in counterfactual_role_gaps.items()
        if gaps
    }

    return {
        "catalog_sha256": _sha256(catalog_path),
        "schema_sha256": _sha256(SCHEMA_PATH),
        "policy_sha256": _sha256(POLICY_PATH),
        "rubric_sha256": _sha256(RUBRIC_PATH),
        "policy_version": policy["policy_version"],
        "episode_count": len(episodes),
        "source_pool_counts": dict(sorted(source_counts.items())),
        "review_status_counts": dict(sorted(review_counts.items())),
        "multi_turn_episode_count": multi_turn_count,
        "high_or_critical_risk_episode_count": critical_risk_count,
        "coverage": coverage,
        "policy_gaps": {
            "source_pool_gaps": source_gaps,
            "fully_reviewed_episode_gap": reviewed_gap,
            "multi_turn_episode_gap": multi_turn_gap,
            "high_or_critical_risk_episode_gap": critical_risk_gap,
            "counterfactual_role_gaps": counterfactual_role_gaps,
            "missing_coverage": missing_coverage,
        },
    }


def validate_catalog(
    catalog_path: Path, *, require_policy_ready: bool
) -> tuple[list[str], dict[str, Any]]:
    schema = _load_json(SCHEMA_PATH)
    policy = _load_json(POLICY_PATH)
    rubric = _load_json(RUBRIC_PATH)
    readiness = _load_json(READINESS_PATH)
    catalog = _load_json(catalog_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    findings = [
        _format_error(error)
        for error in sorted(validator.iter_errors(catalog), key=lambda item: list(item.path))
    ]
    if findings:
        return findings, {}

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

    rubric_dimensions = {
        dimension["dimension"] for dimension in rubric["dimensions"]
    }
    unknown_rubric_dimensions = sorted(
        {
            dimension
            for episode in episodes
            for dimension in episode["grading"]["semantic_rubric_dimensions"]
            if dimension not in rubric_dimensions
        }
    )
    if unknown_rubric_dimensions:
        findings.append(
            "unknown grader rubric dimensions: {}".format(
                ", ".join(unknown_rubric_dimensions)
            )
        )

    for episode in episodes:
        turns = [
            message["turn"] for message in episode["user_episode"]["messages"]
        ]
        if turns != sorted(set(turns)):
            findings.append(
                "{} user message turns must be unique and increasing".format(
                    episode["episode_id"]
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
                    episode["episode_id"],
                    episode["source_pool"],
                    expected_authoring_method,
                )
            )
        if episode["source_pool"] == "real_user_language":
            if actual_authoring_method not in {
                "domain_interview",
                "redacted_production_trace",
            }:
                findings.append(
                    "{} real_user_language requires interview or trace provenance".format(
                        episode["episode_id"]
                    )
                )
            if not episode["provenance"].get("source_trace_ref"):
                findings.append(
                    "{} real_user_language requires source_trace_ref".format(
                        episode["episode_id"]
                    )
                )
        attested_roles = {
            attestation["reviewer_role"]
            for attestation in episode["provenance"]["review_attestations"]
        }
        expected_roles = {
            "candidate": set(),
            "business_reviewed": {"business_owner"},
            "measurement_reviewed": {"measurement_reviewer"},
            "fully_reviewed": {
                "business_owner",
                "measurement_reviewer",
            },
        }[episode["provenance"]["review_status"]]
        if attested_roles != expected_roles:
            findings.append(
                "{} review status {} requires attestations {}".format(
                    episode["episode_id"],
                    episode["provenance"]["review_status"],
                    sorted(expected_roles),
                )
            )
        reviewer_refs = [
            attestation["reviewer_ref"]
            for attestation in episode["provenance"]["review_attestations"]
        ]
        if len(reviewer_refs) != len(set(reviewer_refs)):
            findings.append(
                "{} review attestations require independent reviewers".format(
                    episode["episode_id"]
                )
            )
        reviewable_hash = _reviewable_episode_hash(episode)
        for attestation in episode["provenance"]["review_attestations"]:
            if attestation["reviewed_content_hash"] != reviewable_hash:
                findings.append(
                    "{} review attestation {} targets stale content".format(
                        episode["episode_id"],
                        attestation["review_record_ref"],
                    )
                )

    report = _coverage_report(episodes, policy, catalog_path=catalog_path)
    if catalog_path.resolve() == AUTHORING_CATALOG_PATH.resolve():
        expected_hashes = readiness["evaluated_artifacts"]
        actual_hashes = {
            "catalog_sha256": report["catalog_sha256"],
            "schema_sha256": report["schema_sha256"],
            "policy_sha256": report["policy_sha256"],
            "rubric_sha256": report["rubric_sha256"],
        }
        for hash_name, actual_hash in actual_hashes.items():
            if expected_hashes[hash_name] != actual_hash:
                findings.append(
                    "readiness record has stale {}: expected {}, got {}".format(
                        hash_name,
                        expected_hashes[hash_name],
                        actual_hash,
                    )
                )
        report["readiness_record_sha256"] = _sha256(READINESS_PATH)
        report["policy_gaps"]["open_adversarial_findings"] = readiness[
            "open_adversarial_findings"
        ]
        report["policy_gaps"]["missing_required_artifacts"] = readiness[
            "missing_required_artifacts"
        ]
    policy_ready = all(
        value in ({}, 0)
        for value in report["policy_gaps"].values()
    )
    report["policy_ready"] = policy_ready
    report["readiness_status"] = (
        "policy_ready" if policy_ready else "blocked_by_explicit_gaps"
    )
    if require_policy_ready:
        gaps = report["policy_gaps"]
        for name, value in gaps.items():
            if value not in ({}, 0):
                findings.append("policy gap {}: {}".format(name, value))
    return findings, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path)
    parser.add_argument(
        "--require-policy-ready",
        action="store_true",
        help="fail when the catalog has coverage or review gaps",
    )
    report_group = parser.add_mutually_exclusive_group()
    report_group.add_argument(
        "--report",
        type=Path,
        help="write the computed coverage report",
    )
    report_group.add_argument(
        "--check-report",
        type=Path,
        help="fail when the checked-in coverage report is missing or stale",
    )
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
