#!/usr/bin/env python3
"""Fail-closed verifier for Gate 3 E0 promotion readiness."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from build_gate3_eval_corpus import _expected_artifacts, _render
from compile_gate3_eval_views import validate_all_views
from validate_gate3_eval_catalog import (
    AUTHORING_CATALOG_PATH,
    canonical_sha256,
    claim_ceiling_allows,
    episode_core,
    validate_catalog,
    validate_counterfactual_materialization,
)
from validate_gate3_eval_result import contract_self_test


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
EVAL_ROOT = ROOT / "evals" / "gate3"
TRUST_SCHEMA_PATH = EVAL_ROOT / "gate3-e0-trust.schema.json"
REVIEW_PACKAGE_SCHEMA_PATH = EVAL_ROOT / "review-package.schema.json"
EPISODE_SCHEMA_PATH = EVAL_ROOT / "evaluation-episode.schema.json"
POLICY_SCHEMA_PATH = EVAL_ROOT / "gate3-eval-policy.schema.json"
READINESS_PATH = EVAL_ROOT / "gate3-e0-readiness.json"
COVERAGE_LEDGER_PATH = EVAL_ROOT / "coverage-ledger.json"
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
SOURCE_REGISTRY_PATH = EVAL_ROOT / "registries" / "source-registry.json"
REVIEW_REGISTRY_PATH = EVAL_ROOT / "registries" / "review-registry.json"
CORPUS_REGISTRY_PATH = EVAL_ROOT / "registries" / "corpus-registry.json"
GRADER_REGISTRY_PATH = EVAL_ROOT / "registries" / "grader-registry.json"
AUTHORITY_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "authority-conformance-profiles.json"
)
WORLD_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "cross-gate-world-profiles.json"
)
TAXONOMY_PATH = EVAL_ROOT / "taxonomy" / "coverage-taxonomy.json"
PROMOTION_MANIFEST_PATH = (
    EVAL_ROOT / "manifests" / "promotion-manifest.json"
)
HELD_OUT_MANIFEST_PATH = (
    EVAL_ROOT / "manifests" / "protected-held-out-manifest.json"
)
CALIBRATION_PACKAGE_PATH = (
    EVAL_ROOT / "calibration" / "grader-calibration-package.json"
)
RUN_MANIFEST_PATH = EVAL_ROOT / "manifests" / "run-manifest.json"
REVIEW_PACKAGES_PATH = EVAL_ROOT / "promotion" / "review-packages.json"
VIEW_SCHEMA_PATH = EVAL_ROOT / "evaluation-views.schema.json"
RESULT_SCHEMA_PATH = EVAL_ROOT / "evaluation-run-result.schema.json"
VERIFIER_CODE_PATHS = (
    ROOT / "tools" / "build_gate3_eval_corpus.py",
    ROOT / "tools" / "compile_gate3_eval_views.py",
    ROOT / "tools" / "validate_gate3_eval_catalog.py",
    ROOT / "tools" / "validate_gate3_eval_result.py",
    ROOT / "tools" / "verify_gate3_e0.py",
    ROOT / "tools" / "assert_gate3_1_entry.py",
)

SOURCE_AUTHORITY_REQUIREMENTS = {
    "real_user_language": ("task_transcript", "user_wording"),
    "expert_business_case": ("expert_authorship", "business_world"),
    "historical_failure": (
        "incident_reconstruction",
        "historical_failure_identity",
    ),
    "generated_business_world": (
        "controlled_generation",
        "authoring_method",
    ),
    "adversarial_conversation": (
        "adversarial_generation",
        "authoring_method",
    ),
}
REQUIRED_REVIEW_SCOPES = {
    "business_owner": {
        "user wording and decision target",
        "decision stakes and forbidden harms",
        "business-world realism",
        "required disposition and boundary usefulness",
        "source pool and coverage classification",
    },
    "measurement_reviewer": {
        "truth identifiability and support",
        "valid design families",
        "calendar and business-day semantics",
        "counterfactual atomicity and relation",
        "claim ceiling by estimand and claim target",
        "source pool and coverage classification",
    },
}

TRUST_ARTIFACT_PATHS = (
    TAXONOMY_PATH,
    SOURCE_REGISTRY_PATH,
    REVIEW_REGISTRY_PATH,
    CORPUS_REGISTRY_PATH,
    GRADER_REGISTRY_PATH,
    AUTHORITY_PROFILES_PATH,
    WORLD_PROFILES_PATH,
    PROMOTION_MANIFEST_PATH,
    HELD_OUT_MANIFEST_PATH,
    CALIBRATION_PACKAGE_PATH,
    RUN_MANIFEST_PATH,
)
def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_workspace_ref(
    workspace_root: Path, reference: str
) -> Path | None:
    reference_path = Path(reference)
    if reference_path.is_absolute():
        return None
    root = workspace_root.resolve()
    resolved = (root / reference_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _condition(
    condition_id: str, passed: bool, *evidence: str
) -> dict[str, Any]:
    return {
        "condition_id": condition_id,
        "verdict": "pass" if passed else "blocked",
        "evidence": list(evidence),
    }


def _evaluated_file_sha256(path: Path) -> str:
    if path.suffix == ".json":
        return canonical_sha256(_load_json(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_findings(
    value: Any, schema: Mapping[str, Any], *, label: str
) -> list[str]:
    return [
        "{} {}: {}".format(
            label,
            "/".join(str(part) for part in error.absolute_path) or "<root>",
            error.message,
        )
        for error in Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(value)
    ]


def _authority_root_bundle(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        root_kind: policy["corpus_authority"][root_kind]
        for root_kind in (
            "reviewer_authority_roots",
            "source_authority_roots",
            "manifest_authority_roots",
        )
    }


def _valid_reviews(
    episodes_by_id: Mapping[str, Mapping[str, Any]],
    corpus_entries: Mapping[str, Mapping[str, Any]],
    review_registry: Mapping[str, Any],
    review_packages: Mapping[str, Any],
    reviewer_authority_roots: Mapping[str, Mapping[str, Any]],
    authorized_attestation_sha256s: set[str] | None = None,
) -> tuple[dict[str, set[str]], set[str], list[str]]:
    authorized_attestation_sha256s = (
        authorized_attestation_sha256s or set()
    )
    principals = {
        principal["principal_id"]: principal
        for principal in review_registry["principals"]
    }
    approved_roles: dict[str, set[str]] = {}
    valid_record_ids: set[str] = set()
    reviewer_by_episode: dict[str, dict[str, str]] = {}
    findings: list[str] = []
    packages_by_episode = {
        package["episode_id"]: package
        for package in review_packages["packages"]
    }
    for record in review_registry["records"]:
        episode_id = record["episode_id"]
        principal = principals.get(record["principal_id"])
        if principal is None or principal["status"] != "active":
            findings.append(
                "{} has unknown or inactive reviewer {}".format(
                    record["review_record_id"], record["principal_id"]
                )
            )
            continue
        if record["reviewer_role"] not in principal["roles"]:
            findings.append(
                "{} reviewer is not authorized for {}".format(
                    record["review_record_id"], record["reviewer_role"]
                )
            )
            continue
        authority_root = reviewer_authority_roots.get(
            principal["authority_root_ref"]
        )
        if (
            authority_root is None
            or authority_root["principal_id"] != principal["principal_id"]
            or record["reviewer_role"]
            not in authority_root["authorized_roles"]
        ):
            findings.append(
                "{} reviewer lacks a trusted authority root".format(
                    record["review_record_id"]
                )
            )
            continue
        package = packages_by_episode.get(episode_id)
        if (
            package is None
            or record["review_package_sha256"]
            != canonical_sha256(package)
        ):
            findings.append(
                "{} targets a missing or stale review package".format(
                    record["review_record_id"]
                )
            )
            continue
        missing_scopes = REQUIRED_REVIEW_SCOPES[
            record["reviewer_role"]
        ] - set(record["reviewed_scopes"])
        if missing_scopes:
            findings.append(
                "{} omits required review scopes {}".format(
                    record["review_record_id"], sorted(missing_scopes)
                )
            )
            continue
        episode = episodes_by_id.get(episode_id)
        if episode is None:
            findings.append(
                "{} cites unknown episode {}".format(
                    record["review_record_id"], episode_id
                )
            )
            continue
        expected_hash = corpus_entries[episode_id]["episode_core_sha256"]
        if record["episode_core_sha256"] != expected_hash:
            findings.append(
                "{} targets stale Episode content".format(
                    record["review_record_id"]
                )
            )
            continue
        if record["decision"] != "approved":
            continue
        if canonical_sha256(record) not in authorized_attestation_sha256s:
            findings.append(
                "{} lacks protected external attestation".format(
                    record["review_record_id"]
                )
            )
            continue
        valid_record_ids.add(record["review_record_id"])
        role_reviewers = reviewer_by_episode.setdefault(episode_id, {})
        role_reviewers[record["reviewer_role"]] = record["principal_id"]
        approved_roles.setdefault(episode_id, set()).add(
            record["reviewer_role"]
        )
    for episode_id, role_reviewers in reviewer_by_episode.items():
        if (
            set(role_reviewers) == {"business_owner", "measurement_reviewer"}
            and len(set(role_reviewers.values())) != 2
        ):
            findings.append(
                "{} business and measurement reviews must be independent".format(
                    episode_id
                )
            )
            approved_roles[episode_id] = set()
    return approved_roles, valid_record_ids, findings


def _source_binding_status(
    episodes_by_id: Mapping[str, Mapping[str, Any]],
    corpus_entries: Mapping[str, Mapping[str, Any]],
    source_registry: Mapping[str, Any],
    policy: Mapping[str, Any],
    source_authority_roots: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    authorized_attestation_sha256s: set[str] | None = None,
    workspace_root: Path = WORKSPACE_ROOT,
) -> tuple[
    dict[str, int],
    dict[str, int],
    dict[str, set[str]],
    list[str],
]:
    source_records = {
        record["source_record_id"]: record
        for record in source_registry["records"]
    }
    source_authority_roots = source_authority_roots or {}
    authorized_attestation_sha256s = (
        authorized_attestation_sha256s or set()
    )
    verified_source_episode_counts: Counter[str] = Counter()
    verified_independent_sources: dict[str, set[str]] = {}
    findings: list[str] = []
    source_artifact_text: dict[str, str] = {}
    trusted_source_ids: set[str] = set()
    for source in source_registry["records"]:
        finding_count_before = len(findings)
        required_provenance, required_scope = SOURCE_AUTHORITY_REQUIREMENTS[
            source["source_pool"]
        ]
        if source["provenance_kind"] != required_provenance:
            findings.append(
                "{} has invalid provenance kind for {}".format(
                    source["source_record_id"], source["source_pool"]
                )
            )
        if required_scope not in source["attested_scope"]:
            findings.append(
                "{} lacks required attested scope {}".format(
                    source["source_record_id"], required_scope
                )
            )
        if (
            source["verification_status"] == "verified"
            and source["independent_source_key"].startswith("pending:")
        ):
            findings.append(
                "{} has a pending independent source key".format(
                    source["source_record_id"]
                )
            )
        if source["verification_status"] == "verified":
            authority_root = source_authority_roots.get(
                source.get("verification_authority_ref")
            )
            if (
                authority_root is None
                or source["source_pool"]
                not in authority_root["authorized_source_pools"]
            ):
                findings.append(
                    "{} lacks a trusted source authority root".format(
                        source["source_record_id"]
                    )
                )
            if (
                canonical_sha256(source)
                not in authorized_attestation_sha256s
            ):
                findings.append(
                    "{} lacks protected external attestation".format(
                        source["source_record_id"]
                    )
                )
        for source_artifact in source["source_artifacts"]:
            artifact_path = _resolve_workspace_ref(
                workspace_root, source_artifact["ref"]
            )
            if artifact_path is None or not artifact_path.exists():
                findings.append(
                    "{} source artifact is missing: {}".format(
                        source["source_record_id"], source_artifact["ref"]
                    )
                )
                continue
            actual_sha256 = hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest()
            if actual_sha256 != source_artifact["content_sha256"]:
                findings.append(
                    "{} source artifact hash is stale: {}".format(
                        source["source_record_id"], source_artifact["ref"]
                    )
                )
            source_artifact_text[source["source_record_id"]] = (
                source_artifact_text.get(source["source_record_id"], "")
                + artifact_path.read_text(encoding="utf-8")
            )
        if (
            source["verification_status"] == "verified"
            and len(findings) == finding_count_before
        ):
            trusted_source_ids.add(source["source_record_id"])
    for episode_id, episode in episodes_by_id.items():
        entry = corpus_entries.get(episode_id)
        if entry is None:
            continue
        source = source_records.get(entry["source_record_ref"])
        if source is None:
            findings.append("{} source record is missing".format(episode_id))
            continue
        if source["source_pool"] != episode["source_pool"]:
            findings.append(
                "{} source pool does not match registry".format(episode_id)
            )
            continue
        episode_source_valid = source["source_record_id"] in trusted_source_ids
        if source["verification_status"] != "verified":
            findings.append(
                "{} source {} is {}".format(
                    episode_id,
                    source["source_record_id"],
                    source["verification_status"],
                )
            )
        if (
            episode["source_pool"] == "real_user_language"
            and source.get("privacy_status") != "approved_redacted"
        ):
            findings.append(
                "{} real-user privacy/source stewardship is pending".format(
                    episode_id
                )
            )
        if (
            episode["source_pool"] == "real_user_language"
            and episode["user_episode"]["messages"][0]["text"]
            not in source_artifact_text.get(source["source_record_id"], "")
        ):
            findings.append(
                "{} user wording is absent from the source artifact".format(
                    episode_id
                )
            )
            episode_source_valid = False
        if (
            episode["source_pool"] == "real_user_language"
            and source.get("privacy_status") != "approved_redacted"
        ):
            episode_source_valid = False
        if episode_source_valid:
            verified_source_episode_counts[episode["source_pool"]] += 1
            verified_independent_sources.setdefault(
                episode["source_pool"], set()
            ).add(source["independent_source_key"])
    source_pool_gaps = {
        source_pool: minimum
        - verified_source_episode_counts[source_pool]
        for source_pool, minimum in policy["minimum_catalog"][
            "source_pool_minimums"
        ].items()
        if verified_source_episode_counts[source_pool] < minimum
    }
    independent_source_gaps = {
        source_pool: minimum
        - len(verified_independent_sources.get(source_pool, set()))
        for source_pool, minimum in policy["minimum_catalog"][
            "independent_sources_per_pool"
        ].items()
        if len(verified_independent_sources.get(source_pool, set())) < minimum
    }
    return (
        source_pool_gaps,
        independent_source_gaps,
        verified_independent_sources,
        findings,
    )


def _artifact_freshness_findings() -> list[str]:
    findings: list[str] = []
    for path, expected in _expected_artifacts().items():
        if not path.exists():
            findings.append("missing generated artifact {}".format(path))
        elif path.read_text(encoding="utf-8") != _render(expected):
            findings.append("stale generated artifact {}".format(path))
    return findings


def _authority_roots(
    policy: Mapping[str, Any],
    *,
    workspace_root: Path = WORKSPACE_ROOT,
) -> tuple[dict[str, dict[str, Mapping[str, Any]]], list[str]]:
    root_bundle = _authority_root_bundle(policy)
    roots: dict[str, dict[str, Mapping[str, Any]]] = {
        root_kind: {} for root_kind in root_bundle
    }
    findings: list[str] = []
    configured_root_count = sum(
        len(configured_roots)
        for configured_roots in root_bundle.values()
    )
    if configured_root_count:
        findings.append(
            "external admission verifier is not configured; local authority roots are denied"
        )
        return roots, findings
    for root_kind, configured_roots in root_bundle.items():
        for root in configured_roots:
            root_id = root["authority_root_id"]
            if root_id in roots[root_kind]:
                findings.append(
                    "{} contains duplicate {}".format(root_kind, root_id)
                )
                continue
            receipt_path = _resolve_workspace_ref(
                workspace_root, root["receipt_ref"]
            )
            if receipt_path is None or not receipt_path.exists():
                findings.append(
                    "{} authority receipt is missing".format(root_id)
                )
                continue
            if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != root[
                "receipt_sha256"
            ]:
                findings.append(
                    "{} authority receipt hash is stale".format(root_id)
                )
                continue
            roots[root_kind][root_id] = root
    return roots, findings


def _manifest_authorized(
    artifact: Mapping[str, Any],
    roots: Mapping[str, Mapping[str, Any]],
    authorized_manifest_sha256s: set[str],
) -> bool:
    root = roots.get(artifact.get("authority_root_ref"))
    return (
        root is not None
        and artifact["artifact_type"] in root["authorized_artifact_types"]
        and canonical_sha256(artifact) in authorized_manifest_sha256s
    )


def _has_valid_predecessor(
    artifact: Mapping[str, Any],
    *,
    authorized_manifest_sha256s: set[str],
    trust_schema: Mapping[str, Any],
    workspace_root: Path = WORKSPACE_ROOT,
    visited_paths: set[Path] | None = None,
) -> bool:
    transition_sources = {
        "promotion_manifest": ("draft", "approved"),
        "protected_held_out_manifest": ("unsealed", "sealed"),
        "grader_calibration_package": ("pending", "calibrated"),
        "run_manifest": ("draft", "frozen"),
    }
    initial_status, final_status = transition_sources.get(
        artifact.get("artifact_type"), (None, None)
    )
    if (
        initial_status is None
        or artifact.get("status") not in {initial_status, final_status}
        or not artifact.get("authority_root_ref")
    ):
        return False
    history = artifact.get("authority_history")
    if artifact["registry_epoch"] == 1:
        return (
            history is None
            and artifact["status"] == initial_status
            and canonical_sha256(artifact)
            in authorized_manifest_sha256s
        )
    if history is None:
        return False
    predecessor_path = _resolve_workspace_ref(
        workspace_root, history["predecessor_ref"]
    )
    if predecessor_path is None or not predecessor_path.exists():
        return False
    visited_paths = set() if visited_paths is None else set(visited_paths)
    resolved_predecessor_path = predecessor_path.resolve()
    if resolved_predecessor_path in visited_paths:
        return False
    visited_paths.add(resolved_predecessor_path)
    try:
        predecessor = _load_json(predecessor_path)
    except (OSError, ValueError):
        return False
    predecessor_sha256 = canonical_sha256(predecessor)
    if predecessor_sha256 != history["predecessor_sha256"]:
        return False
    if predecessor_sha256 not in authorized_manifest_sha256s:
        return False
    if _schema_findings(
        predecessor, trust_schema, label=predecessor_path.name
    ):
        return False
    predecessor_epoch = predecessor.get("registry_epoch")
    if not (
        predecessor.get("artifact_type") == artifact["artifact_type"]
        and isinstance(predecessor_epoch, int)
        and predecessor_epoch + 1 == artifact["registry_epoch"]
        and predecessor.get("authority_root_ref")
        == artifact["authority_root_ref"]
        and predecessor.get("status")
        in (
            {initial_status}
            if artifact["status"] == initial_status
            else {initial_status, final_status}
        )
    ):
        return False
    return _has_valid_predecessor(
        predecessor,
        authorized_manifest_sha256s=authorized_manifest_sha256s,
        trust_schema=trust_schema,
        workspace_root=workspace_root,
        visited_paths=visited_paths,
    )


def _promotion_findings(
    promotion_manifest: Mapping[str, Any],
    episodes_by_id: Mapping[str, Mapping[str, Any]],
    corpus_entries: Mapping[str, Mapping[str, Any]],
    source_registry: Mapping[str, Any],
    review_registry: Mapping[str, Any],
    valid_review_record_ids: set[str],
) -> tuple[set[str], list[str]]:
    findings: list[str] = []
    promoted_ids: set[str] = set()
    source_records = {
        record["source_record_id"]: record
        for record in source_registry["records"]
    }
    principals = {
        principal["principal_id"]: principal
        for principal in review_registry["principals"]
    }
    review_records = {
        record["review_record_id"]: record
        for record in review_registry["records"]
    }
    if len(review_records) != len(review_registry["records"]):
        findings.append("review registry contains duplicate record ids")
    for entry in promotion_manifest["entries"]:
        episode_id = entry["episode_id"]
        if episode_id in promoted_ids:
            findings.append(
                "{} appears more than once in promotion manifest".format(
                    episode_id
                )
            )
            continue
        promoted_ids.add(episode_id)
        episode = episodes_by_id.get(episode_id)
        corpus_entry = corpus_entries.get(episode_id)
        if episode is None or corpus_entry is None:
            findings.append(
                "{} promotion targets an unknown Episode".format(episode_id)
            )
            continue
        for field in (
            "episode_core_sha256",
            "source_record_ref",
            "world_profile_ref",
            "authority_profile_ref",
        ):
            if entry[field] != corpus_entry[field]:
                findings.append(
                    "{} promotion has stale {}".format(episode_id, field)
                )
        if (
            entry["grader_profile_ref"]
            != corpus_entry["product_grader_profile_ref"]
        ):
            findings.append(
                "{} promotion has stale grader_profile_ref".format(
                    episode_id
                )
            )
        source = source_records.get(entry["source_record_ref"])
        if source is None or source["verification_status"] != "verified":
            findings.append(
                "{} promotion source is not verified".format(episode_id)
            )
        reviewer_principals: dict[str, str] = {}
        for role, ref_field in (
            ("business_owner", "business_review_ref"),
            ("measurement_reviewer", "measurement_review_ref"),
        ):
            record = review_records.get(entry[ref_field])
            if (
                record is None
                or entry[ref_field] not in valid_review_record_ids
                or record["episode_id"] != episode_id
                or record["episode_core_sha256"]
                != corpus_entry["episode_core_sha256"]
                or record["reviewer_role"] != role
                or record["decision"] != "approved"
            ):
                findings.append(
                    "{} promotion has invalid {}".format(
                        episode_id, ref_field
                    )
                )
                continue
            principal = principals.get(record["principal_id"])
            if (
                principal is None
                or principal["status"] != "active"
                or role not in principal["roles"]
            ):
                findings.append(
                    "{} promotion has unauthorized {}".format(
                        episode_id, ref_field
                    )
                )
                continue
            reviewer_principals[role] = record["principal_id"]
        if (
            len(reviewer_principals) == 2
            and len(set(reviewer_principals.values())) != 2
        ):
            findings.append(
                "{} promotion review principals are not independent".format(
                    episode_id
                )
            )
    return promoted_ids, findings


def compute_readiness() -> tuple[dict[str, Any], list[str]]:
    findings: list[str] = []
    catalog_findings, authoring_report = validate_catalog(
        AUTHORING_CATALOG_PATH,
        require_policy_ready=False,
    )
    findings.extend(catalog_findings)
    if (
        not COVERAGE_LEDGER_PATH.exists()
        or _load_json(COVERAGE_LEDGER_PATH) != authoring_report
    ):
        findings.append("coverage ledger is missing or stale")
    catalog = _load_json(AUTHORING_CATALOG_PATH)
    policy = _load_json(POLICY_PATH)
    taxonomy = _load_json(TAXONOMY_PATH)
    source_registry = _load_json(SOURCE_REGISTRY_PATH)
    review_registry = _load_json(REVIEW_REGISTRY_PATH)
    corpus = _load_json(CORPUS_REGISTRY_PATH)
    grader_registry = _load_json(GRADER_REGISTRY_PATH)
    authority_profiles = _load_json(AUTHORITY_PROFILES_PATH)
    world_profiles = _load_json(WORLD_PROFILES_PATH)
    promotion_manifest = _load_json(PROMOTION_MANIFEST_PATH)
    held_out_manifest = _load_json(HELD_OUT_MANIFEST_PATH)
    calibration = _load_json(CALIBRATION_PACKAGE_PATH)
    run_manifest = _load_json(RUN_MANIFEST_PATH)
    review_packages = _load_json(REVIEW_PACKAGES_PATH)
    authorized_attestation_sha256s: set[str] = set()
    authorized_manifest_sha256s: set[str] = set()
    external_admission_verified = False
    authority_roots, authority_root_findings = _authority_roots(policy)
    findings.extend(authority_root_findings)

    trust_schema = _load_json(TRUST_SCHEMA_PATH)
    for path in TRUST_ARTIFACT_PATHS:
        findings.extend(
            _schema_findings(
                _load_json(path), trust_schema, label=path.name
            )
        )
    findings.extend(
        _schema_findings(
            review_packages,
            _load_json(REVIEW_PACKAGE_SCHEMA_PATH),
            label=REVIEW_PACKAGES_PATH.name,
        )
    )
    for schema_path in (VIEW_SCHEMA_PATH, RESULT_SCHEMA_PATH):
        try:
            Draft202012Validator.check_schema(_load_json(schema_path))
        except Exception as error:
            findings.append("{} invalid schema: {}".format(schema_path, error))

    freshness_findings = _artifact_freshness_findings()
    findings.extend(freshness_findings)
    view_findings = validate_all_views()
    findings.extend(view_findings)
    result_contract_findings = contract_self_test()
    findings.extend(result_contract_findings)

    episodes_by_id = {
        episode["episode_id"]: episode for episode in catalog["episodes"]
    }
    corpus_entries = {
        entry["episode_id"]: entry for entry in corpus["entries"]
    }
    if set(episodes_by_id) != set(corpus_entries):
        findings.append("corpus registry does not exactly cover the catalog")
    for episode_id, episode in episodes_by_id.items():
        entry = corpus_entries.get(episode_id)
        if entry is None:
            continue
        if entry["episode_core_sha256"] != canonical_sha256(
            episode_core(episode)
        ):
            findings.append("{} corpus hash is stale".format(episode_id))

    (
        source_pool_gaps,
        independent_source_gaps,
        verified_independent_sources,
        source_binding_findings,
    ) = _source_binding_status(
        episodes_by_id,
        corpus_entries,
        source_registry,
        policy,
        authority_roots["source_authority_roots"],
        authorized_attestation_sha256s=(
            authorized_attestation_sha256s
        ),
    )

    approved_roles, valid_review_record_ids, review_findings = _valid_reviews(
        episodes_by_id,
        corpus_entries,
        review_registry,
        review_packages,
        authority_roots["reviewer_authority_roots"],
        authorized_attestation_sha256s,
    )
    findings.extend(review_findings)
    fully_reviewed_ids = {
        episode_id
        for episode_id, roles in approved_roles.items()
        if roles == {"business_owner", "measurement_reviewer"}
    }
    reviewed_gap = max(
        0,
        policy["minimum_catalog"]["reviewed_base_episodes"]
        - len(fully_reviewed_ids),
    )

    reviewed_coverage = {
        dimension: {
            value
            for episode_id in fully_reviewed_ids
            for value in corpus_entries[episode_id]["coverage_tags"][
                dimension
            ]
        }
        for dimension in taxonomy["dimensions"]
    }
    reviewed_coverage_gaps = {
        dimension: sorted(set(values) - reviewed_coverage[dimension])
        for dimension, values in taxonomy["dimensions"].items()
        if set(values) - reviewed_coverage[dimension]
    }

    pending_truth_count = sum(
        truth["identifiability"] == "pending_independent_review"
        for episode in catalog["episodes"]
        for truth in episode["business_world"]["truth_facts"]
    )
    claim_structure_gaps: list[str] = []
    counterfactual_execution_gaps: list[str] = []
    for episode in catalog["episodes"]:
        estimands = episode["acceptable_outcome"].get("estimands", [])
        claim_targets = episode["acceptable_outcome"].get(
            "claim_targets", []
        )
        estimand_ids = [item["estimand_id"] for item in estimands]
        claim_target_ids = [
            item["claim_target_id"] for item in claim_targets
        ]
        claimed_estimands = [
            item["estimand_id"] for item in claim_targets
        ]
        claim_targets_by_id = {
            item["claim_target_id"]: item for item in claim_targets
        }
        boundary_claim_gaps = any(
            not authorization.get("claim_target_ids")
            or set(authorization["claim_target_ids"])
            - set(claim_targets_by_id)
            or any(
                not claim_ceiling_allows(
                    claim_targets_by_id[target_id]["claim_ceiling"],
                    authorization["maximum_claim_ceiling"],
                )
                for target_id in authorization.get(
                    "claim_target_ids", []
                )
                if target_id in claim_targets_by_id
            )
            for authorization in episode["support_expectation"][
                "boundary_authorizations"
            ]
        )
        tagged_multi = (
            "multi_estimand"
            in episode["coverage_tags"]["measurement_challenges"]
        )
        if (
            not estimand_ids
            or len(estimand_ids) != len(set(estimand_ids))
            or len(claim_target_ids) != len(set(claim_target_ids))
            or set(claimed_estimands) != set(estimand_ids)
            or tagged_multi != (len(estimand_ids) > 1)
            or boundary_claim_gaps
            or any(
                not claim_ceiling_allows(
                    episode["acceptable_outcome"]["claim_ceiling"],
                    target["claim_ceiling"],
                )
                for target in claim_targets
            )
        ):
            claim_structure_gaps.append(episode["episode_id"])
        for sibling in episode["counterfactual_siblings"]:
            if validate_counterfactual_materialization(
                episode, sibling
            ):
                counterfactual_execution_gaps.append(
                    sibling["sibling_id"]
                )

    grader_profile_ids = {
        profile["profile_id"] for profile in grader_registry["profiles"]
    }
    invariant_ids = {
        invariant["invariant_id"]
        for invariant in grader_registry["authority_invariants"]
    }
    authority_profile_by_id = {
        profile["profile_id"]: profile
        for profile in authority_profiles["profiles"]
    }
    authority_binding_by_episode = {
        binding["episode_id"]: binding
        for binding in authority_profiles["bindings"]
    }
    profile_findings: list[str] = []
    for entry in corpus["entries"]:
        if entry["product_grader_profile_ref"] not in grader_profile_ids:
            profile_findings.append(
                "{} product grader profile is missing".format(
                    entry["episode_id"]
                )
            )
        authority_profile = authority_profile_by_id.get(
            entry["authority_profile_ref"]
        )
        authority_binding = authority_binding_by_episode.get(
            entry["episode_id"]
        )
        if authority_profile is None:
            profile_findings.append(
                "{} authority profile is missing".format(entry["episode_id"])
            )
        elif (
            set(authority_profile["required_invariant_ids"]) - invariant_ids
        ):
            profile_findings.append(
                "{} authority profile cites unknown invariants".format(
                    entry["episode_id"]
                )
            )
        if authority_binding is None:
            profile_findings.append(
                "{} authority binding is missing".format(entry["episode_id"])
            )
        elif (
            authority_binding["episode_core_sha256"]
            != entry["episode_core_sha256"]
            or authority_binding["world_profile_ref"]
            != entry["world_profile_ref"]
            or authority_binding["profile_id"]
            != entry["authority_profile_ref"]
        ):
            profile_findings.append(
                "{} authority binding is stale".format(entry["episode_id"])
            )
    world_profile_by_id = {
        profile["profile_id"]: profile for profile in world_profiles["profiles"]
    }
    for entry in corpus["entries"]:
        profile = world_profile_by_id.get(entry["world_profile_ref"])
        if profile is None:
            profile_findings.append(
                "{} world profile is missing".format(entry["episode_id"])
            )
        elif profile["episode_core_sha256"] != entry["episode_core_sha256"]:
            profile_findings.append(
                "{} world profile hash is stale".format(entry["episode_id"])
            )

    promotion_entries = promotion_manifest["entries"]
    promoted_ids, promotion_findings = _promotion_findings(
        promotion_manifest,
        episodes_by_id,
        corpus_entries,
        source_registry,
        review_registry,
        valid_review_record_ids,
    )
    promotion_ready = (
        promotion_manifest["status"] == "approved"
        and len(promoted_ids)
        >= policy["minimum_catalog"]["reviewed_base_episodes"]
        and promoted_ids <= fully_reviewed_ids
        and not promotion_findings
        and _manifest_authorized(
            promotion_manifest,
            authority_roots["manifest_authority_roots"],
            authorized_manifest_sha256s,
        )
        and _has_valid_predecessor(
            promotion_manifest,
            authorized_manifest_sha256s=authorized_manifest_sha256s,
            trust_schema=trust_schema,
        )
    )
    promoted_hashes = {
        episode_id: corpus_entries[episode_id]["episode_core_sha256"]
        for episode_id in promoted_ids
        if episode_id in corpus_entries
    }
    calibration_episode_ids = {
        entry["episode_id"]
        for entry in promotion_entries
        if entry["target_partition"] == "calibration"
    }
    current_grader_hash = canonical_sha256(grader_registry)
    calibration_labels = calibration["label_records"]
    calibration_label_ids = [
        label["episode_id"] for label in calibration_labels
    ]
    recomputed_agreement = (
        sum(
            label["human_verdict"] == label["grader_verdict"]
            for label in calibration_labels
        )
        / len(calibration_labels)
        if calibration_labels
        else None
    )
    recomputed_critical_false_passes = sum(
        label["critical"]
        and label["human_verdict"] != "pass"
        and label["grader_verdict"] == "pass"
        for label in calibration_labels
    )
    calibration_ready = (
        calibration["status"] == "calibrated"
        and len(calibration_label_ids) == len(set(calibration_label_ids))
        and set(calibration["labeled_episode_refs"])
        == set(calibration_label_ids)
        and all(
            label["human_review_ref"] in valid_review_record_ids
            for label in calibration_labels
        )
        and calibration["critical_false_passes"]
        == recomputed_critical_false_passes
        == 0
        and calibration["grader_registry_sha256"] == current_grader_hash
        and set(calibration["labeled_episode_refs"])
        <= calibration_episode_ids
        and len(calibration["labeled_episode_refs"])
        >= policy["run_policy"]["minimum_calibration_episodes"]
        and calibration["agreement"] is not None
        and recomputed_agreement is not None
        and abs(calibration["agreement"] - recomputed_agreement) < 1e-12
        and calibration["agreement"] * 10_000
        >= policy["run_policy"][
            "minimum_grader_human_agreement_basis_points"
        ]
        and _manifest_authorized(
            calibration,
            authority_roots["manifest_authority_roots"],
            authorized_manifest_sha256s,
        )
        and _has_valid_predecessor(
            calibration,
            authorized_manifest_sha256s=authorized_manifest_sha256s,
            trust_schema=trust_schema,
        )
    )
    held_out_ids = [
        entry["opaque_episode_id"]
        for entry in held_out_manifest["entries"]
    ]
    held_out_promotion_refs = [
        entry["promotion_ref"] for entry in held_out_manifest["entries"]
    ]
    held_out_receipt_findings: list[str] = []
    for entry in held_out_manifest["entries"]:
        receipt_path = _resolve_workspace_ref(
            WORKSPACE_ROOT, entry["object_receipt_ref"]
        )
        if receipt_path is None or not receipt_path.exists():
            held_out_receipt_findings.append(
                "{} object receipt is missing".format(
                    entry["opaque_episode_id"]
                )
            )
            continue
        if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != entry[
            "object_receipt_sha256"
        ]:
            held_out_receipt_findings.append(
                "{} object receipt hash is stale".format(
                    entry["opaque_episode_id"]
                )
            )
            continue
        try:
            receipt = _load_json(receipt_path)
        except (OSError, ValueError):
            held_out_receipt_findings.append(
                "{} object receipt is unreadable".format(
                    entry["opaque_episode_id"]
                )
            )
            continue
        expected_receipt = {
            "opaque_episode_id": entry["opaque_episode_id"],
            "encrypted_object_ref": entry["encrypted_object_ref"],
            "ciphertext_sha256": entry["ciphertext_sha256"],
            "promotion_ref": entry["promotion_ref"],
            "access_realm": entry["access_realm"],
            "registry_epoch": held_out_manifest["registry_epoch"],
        }
        if receipt != expected_receipt:
            held_out_receipt_findings.append(
                "{} object receipt binding is invalid".format(
                    entry["opaque_episode_id"]
                )
            )
    held_out_refs_are_external = all(
        "://" in entry["encrypted_object_ref"]
        and not entry["encrypted_object_ref"].startswith("file://")
        for entry in held_out_manifest["entries"]
    )
    held_out_ready = (
        held_out_manifest["status"] == "sealed"
        and len(held_out_manifest["entries"])
        >= policy["run_policy"]["minimum_held_out_episodes"]
        and len(held_out_ids) == len(set(held_out_ids))
        and len(held_out_promotion_refs)
        == len(set(held_out_promotion_refs))
        and not set(held_out_ids) & set(episodes_by_id)
        and held_out_refs_are_external
        and not held_out_receipt_findings
        and _manifest_authorized(
            held_out_manifest,
            authority_roots["manifest_authority_roots"],
            authorized_manifest_sha256s,
        )
        and _has_valid_predecessor(
            held_out_manifest,
            authorized_manifest_sha256s=authorized_manifest_sha256s,
            trust_schema=trust_schema,
        )
    )
    current_policy_hash = canonical_sha256(policy)
    current_taxonomy_hash = canonical_sha256(taxonomy)
    current_promotion_hash = canonical_sha256(promotion_manifest)
    run_cell_findings: list[str] = []
    run_cell_ids: set[str] = set()
    run_cell_episode_ids: set[str] = set()
    for cell in run_manifest["run_cells"]:
        cell_id = cell["run_cell_id"]
        episode_id = cell["episode_id"]
        if cell_id in run_cell_ids:
            run_cell_findings.append(
                "run manifest contains duplicate cell {}".format(cell_id)
            )
            continue
        run_cell_ids.add(cell_id)
        run_cell_episode_ids.add(episode_id)
        corpus_entry = corpus_entries.get(episode_id)
        world_profile = world_profile_by_id.get(cell["world_profile_ref"])
        authority_profile = authority_profile_by_id.get(
            cell["authority_profile_ref"]
        )
        if corpus_entry is None or episode_id not in promoted_ids:
            run_cell_findings.append(
                "{} targets an unpromoted Episode".format(cell_id)
            )
            continue
        expected_values = {
            "episode_core_sha256": corpus_entry["episode_core_sha256"],
            "world_profile_ref": corpus_entry["world_profile_ref"],
            "authority_profile_ref": corpus_entry["authority_profile_ref"],
            "product_grader_profile_ref": corpus_entry[
                "product_grader_profile_ref"
            ],
        }
        for field, expected_value in expected_values.items():
            if cell[field] != expected_value:
                run_cell_findings.append(
                    "{} has stale {}".format(cell_id, field)
                )
        if (
            world_profile is None
            or cell["world_profile_sha256"]
            != canonical_sha256(world_profile)
        ):
            run_cell_findings.append(
                "{} has stale world profile hash".format(cell_id)
            )
        if (
            authority_profile is None
            or cell["authority_profile_sha256"]
            != canonical_sha256(authority_profile)
        ):
            run_cell_findings.append(
                "{} has stale authority profile hash".format(cell_id)
            )
    run_manifest_ready = (
        run_manifest["status"] == "frozen"
        and run_manifest["policy_sha256"] == current_policy_hash
        and run_manifest["taxonomy_sha256"] == current_taxonomy_hash
        and run_manifest["grader_registry_sha256"] == current_grader_hash
        and run_manifest["promotion_manifest_sha256"]
        == current_promotion_hash
        and run_manifest["selected_episode_hashes"] == promoted_hashes
        and run_cell_episode_ids == promoted_ids
        and not run_cell_findings
        and _manifest_authorized(
            run_manifest,
            authority_roots["manifest_authority_roots"],
            authorized_manifest_sha256s,
        )
        and _has_valid_predecessor(
            run_manifest,
            authorized_manifest_sha256s=authorized_manifest_sha256s,
            trust_schema=trust_schema,
        )
        and set(run_manifest["required_layers"])
        == {
            "product_behavior",
            "authority_conformance",
            "implementation",
        }
    )

    conditions = [
        _condition(
            "external_admission_verified",
            external_admission_verified,
            "local execution has no trusted issuer/runner identity; external admission design requires user confirmation",
        ),
        _condition(
            "authoring_catalog_valid",
            not catalog_findings,
            "{} authoring Episodes".format(
                authoring_report.get("episode_count", 0)
            ),
        ),
        _condition(
            "generated_artifacts_fresh",
            not freshness_findings,
            "candidate union, corpus, profiles and review packages are deterministic",
        ),
        _condition(
            "agent_evaluator_projection_contract_safe",
            not view_findings,
            "45 whitelist projections validate; CLI cannot emit oracle or caller-selected future turns; runtime process isolation remains G3.2",
        ),
        _condition(
            "source_registry_verified",
            not source_binding_findings
            and not source_pool_gaps
            and not independent_source_gaps,
            "source binding findings: {}".format(len(source_binding_findings)),
            "verified source pool gaps: {}".format(source_pool_gaps),
            "independent source diversity gaps: {}".format(
                independent_source_gaps
            ),
            "verified independent sources: {}".format(
                {
                    source_pool: len(source_keys)
                    for source_pool, source_keys in sorted(
                        verified_independent_sources.items()
                    )
                }
            ),
        ),
        _condition(
            "independent_reviews_complete",
            not review_findings and reviewed_gap == 0,
            "{} independently double-reviewed Episodes".format(
                len(fully_reviewed_ids)
            ),
            "reviewed floor gap: {}".format(reviewed_gap),
        ),
        _condition(
            "reviewed_coverage_complete",
            not reviewed_coverage_gaps,
            "reviewed coverage gaps: {}".format(reviewed_coverage_gaps),
        ),
        _condition(
            "truth_identifiability_reviewed",
            pending_truth_count == 0,
            "pending truth facts: {}".format(pending_truth_count),
        ),
        _condition(
            "per_claim_ceiling_complete",
            not claim_structure_gaps,
            "estimand/claim structure gaps: {}".format(
                len(claim_structure_gaps)
            ),
        ),
        _condition(
            "counterfactual_mutations_executable",
            not counterfactual_execution_gaps,
            "unexecutable counterfactuals: {}".format(
                len(counterfactual_execution_gaps)
            ),
        ),
        _condition(
            "grader_and_authority_profiles_resolve",
            not profile_findings,
            "profile findings: {}".format(len(profile_findings)),
        ),
        _condition(
            "promotion_manifest_approved",
            promotion_ready,
            "promotion status: {}".format(promotion_manifest["status"]),
            "promotion entries: {}".format(len(promotion_entries)),
            "promotion binding findings: {}".format(
                len(promotion_findings)
            ),
        ),
        _condition(
            "grader_calibrated",
            calibration_ready,
            "calibration status: {}".format(calibration["status"]),
        ),
        _condition(
            "held_out_partition_sealed",
            held_out_ready,
            "held-out status: {}".format(held_out_manifest["status"]),
            "held-out entries: {}".format(
                len(held_out_manifest["entries"])
            ),
            "held-out receipt findings: {}".format(
                len(held_out_receipt_findings)
            ),
        ),
        _condition(
            "run_manifest_frozen",
            run_manifest_ready,
            "run manifest status: {}".format(run_manifest["status"]),
            "run-cell binding findings: {}".format(len(run_cell_findings)),
        ),
        _condition(
            "three_layer_result_contract_present",
            not result_contract_findings,
            "product, authority and implementation verdicts use strict AND",
            "contract self-test findings: {}".format(
                len(result_contract_findings)
            ),
        ),
    ]
    derived_status = (
        "ready"
        if not findings
        and all(condition["verdict"] == "pass" for condition in conditions)
        else "blocked"
    )
    evaluated_paths = (
        AUTHORING_CATALOG_PATH,
        COVERAGE_LEDGER_PATH,
        POLICY_PATH,
        EPISODE_SCHEMA_PATH,
        POLICY_SCHEMA_PATH,
        TRUST_SCHEMA_PATH,
        REVIEW_PACKAGE_SCHEMA_PATH,
        VIEW_SCHEMA_PATH,
        RESULT_SCHEMA_PATH,
        *TRUST_ARTIFACT_PATHS,
        REVIEW_PACKAGES_PATH,
        *VERIFIER_CODE_PATHS,
    )
    readiness = {
        "artifact_type": "readiness_manifest",
        "artifact_version": "gate3.e0-readiness.v2",
        "registry_epoch": 1,
        "evaluated_artifact_hashes": {
            (
                str(path.relative_to(EVAL_ROOT))
                if path.is_relative_to(EVAL_ROOT)
                else str(path.relative_to(ROOT))
            ): _evaluated_file_sha256(path)
            for path in evaluated_paths
        },
        "condition_verdicts": conditions,
        "derived_status": derived_status,
        "entry_decision": (
            "allow_g3_1" if derived_status == "ready" else "deny_g3_1"
        ),
    }
    findings.extend(profile_findings)
    return readiness, findings


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-manifest", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--require-ready", action="store_true")
    arguments = parser.parse_args()

    readiness, findings = compute_readiness()
    if arguments.write_manifest:
        READINESS_PATH.write_text(
            json.dumps(readiness, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        if not READINESS_PATH.exists():
            findings.append("readiness manifest is missing")
        elif _load_json(READINESS_PATH) != readiness:
            findings.append("readiness manifest is stale")
    findings.extend(
        _schema_findings(
            readiness,
            _load_json(TRUST_SCHEMA_PATH),
            label=READINESS_PATH.name,
        )
    )
    if arguments.require_ready and readiness["derived_status"] != "ready":
        findings.append("Gate 3 E0 readiness is blocked")
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
