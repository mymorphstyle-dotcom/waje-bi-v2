#!/usr/bin/env python3
"""Fail-closed verifier for Gate 3 E0 promotion readiness."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from build_gate3_eval_corpus import _expected_artifacts, _render
from compile_gate3_eval_views import validate_all_views
from gate3_admission_authority import (
    AdmissionAuthorityConnector,
    AdmissionExpectation,
    VerifiedAdmissionAuthority,
    canonical_file_set_sha256,
)
from validate_gate3_eval_catalog import (
    AUTHORING_CATALOG_PATH,
    MISSING_CONTRACT_BACKLOG_PATH,
    canonical_sha256,
    claim_ceiling_allows,
    episode_core,
    materialize_counterfactual_episode,
    validate_catalog,
    validate_counterfactual_materialization,
)
from validate_gate3_eval_result import contract_self_test, validate_result


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
EVAL_ROOT = ROOT / "evals" / "gate3"
TRUST_SCHEMA_PATH = EVAL_ROOT / "gate3-e0-trust.schema.json"
REVIEW_PACKAGE_SCHEMA_PATH = EVAL_ROOT / "review-package.schema.json"
EPISODE_SCHEMA_PATH = EVAL_ROOT / "evaluation-episode.schema.json"
POLICY_SCHEMA_PATH = EVAL_ROOT / "gate3-eval-policy.schema.json"
GITHUB_ADMISSION_REQUEST_SCHEMA_PATH = (
    EVAL_ROOT / "github-admission-request.schema.json"
)
GITHUB_PROVIDER_STATE_SCHEMA_PATH = (
    EVAL_ROOT / "github-provider-state.schema.json"
)
READINESS_PATH = EVAL_ROOT / "gate3-e0-readiness.json"
COVERAGE_LEDGER_PATH = EVAL_ROOT / "coverage-ledger.json"
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
SOURCE_REGISTRY_PATH = EVAL_ROOT / "registries" / "source-registry.json"
REVIEW_REGISTRY_PATH = EVAL_ROOT / "registries" / "review-registry.json"
CORPUS_REGISTRY_PATH = EVAL_ROOT / "registries" / "corpus-registry.json"
CLAIM_TARGET_AUTHORITY_PATH = (
    EVAL_ROOT / "registries" / "claim-target-authority-registry.json"
)
CLAIM_TARGET_AUTHORITY_SCHEMA_PATH = (
    EVAL_ROOT / "claim-target-authority-registry.schema.json"
)
GRADER_REGISTRY_PATH = EVAL_ROOT / "registries" / "grader-registry.json"
GRADER_RUBRIC_PATH = EVAL_ROOT / "grader-rubric.json"
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
TRANSFER_RESEARCH_PATH = EVAL_ROOT / "research" / "transfer-probes.json"
VIEW_SCHEMA_PATH = EVAL_ROOT / "evaluation-views.schema.json"
RESULT_SCHEMA_PATH = EVAL_ROOT / "evaluation-run-result.schema.json"
RUNNER_ARTIFACT_INDEX_SCHEMA_PATH = (
    EVAL_ROOT / "runner-artifact-index.schema.json"
)
CASE_FILE_AUTHORITY_SCHEMA_PATH = (
    EVAL_ROOT / "case-files" / "case-file-authority.schema.json"
)
CASE_FILE_AUTHORITIES_PATH = (
    EVAL_ROOT / "case-files" / "case-file-authorities.json"
)
AUTHORITY_PACKAGE_ROOT = EVAL_ROOT / "authority"
CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH = (
    AUTHORITY_PACKAGE_ROOT / "controlled-business-fixture.schema.json"
)
REAL_SNAPSHOT_MATERIALIZATION_SCHEMA_PATH = (
    AUTHORITY_PACKAGE_ROOT / "real-snapshot-materialization.schema.json"
)
REAL_SNAPSHOT_MATERIALIZATION_PATHS = tuple(
    sorted(
        (AUTHORITY_PACKAGE_ROOT / "fixtures").glob(
            "g3-real-*.json"
        )
    )
)
CONTROLLED_BUSINESS_FIXTURE_PATHS = tuple(
    path
    for path in sorted((AUTHORITY_PACKAGE_ROOT / "fixtures").glob("*.json"))
    if not path.name.startswith("g3-real-")
    and path.name != "g3-user-008-prior-authority.v1.json"
)
AUTHORITY_PACKAGE_VALIDATOR_PATH = (
    AUTHORITY_PACKAGE_ROOT / "validate_authority_package.py"
)
AUTHORITY_PACKAGE_SCHEMA_PATHS = (
    AUTHORITY_PACKAGE_ROOT / "authority-observation-bundle.schema.json",
    AUTHORITY_PACKAGE_ROOT / "authority-registry.schema.json",
    AUTHORITY_PACKAGE_ROOT / "milestone-schedule.schema.json",
    AUTHORITY_PACKAGE_ROOT / "prior-authority-fixture.schema.json",
)
AUTHORITY_PACKAGE_INSTANCE_PATHS = (
    AUTHORITY_PACKAGE_ROOT / "authority-registry.json",
    AUTHORITY_PACKAGE_ROOT
    / "schedules"
    / "gate3-authority-repair.v1.json",
    AUTHORITY_PACKAGE_ROOT
    / "fixtures"
    / "g3-user-008-prior-authority.v1.json",
    AUTHORITY_PACKAGE_ROOT
    / "examples"
    / "g3-user-008-authority-observation-bundle.v1.json",
)
VERIFIER_CODE_PATHS = (
    ROOT / "tools" / "build_gate3_eval_corpus.py",
    ROOT / "tools" / "compile_gate3_eval_views.py",
    ROOT / "tools" / "validate_gate3_eval_catalog.py",
    ROOT / "tools" / "validate_gate3_eval_result.py",
    ROOT / "tools" / "verify_gate3_e0.py",
    ROOT / "tools" / "assert_gate3_1_entry.py",
    ROOT / "tools" / "gate3_admission_authority.py",
    ROOT / "tools" / "github_gate3_admission.py",
    ROOT / "tools" / "build_gate3_github_admission_request.py",
    ROOT / "tools" / "verify_github_workflow_deployment.py",
)

GITHUB_AUTHORITY_PATHS = (
    ROOT / "ops" / "github" / "workflow-authority-policy.json",
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
        "WAJEgame domain, factor and question-family binding",
        "claim case source and contract boundary",
        "source provenance",
    },
    "measurement_reviewer": {
        "truth identifiability and support",
        "valid design families",
        "calendar and business-day semantics",
        "counterfactual atomicity and relation",
        "claim ceiling by estimand and claim target",
        "WAJEgame domain, factor and question-family binding",
        "claim case source and contract boundary",
        "source provenance",
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

EVALUATED_PATHS = (
    ROOT / ".python-version",
    ROOT / "package.json",
    ROOT / "package-lock.json",
    ROOT / "pyproject.toml",
    ROOT / "uv.lock",
    ROOT / "tools" / "isolation-policy.json",
    MISSING_CONTRACT_BACKLOG_PATH,
    AUTHORING_CATALOG_PATH,
    COVERAGE_LEDGER_PATH,
    POLICY_PATH,
    EPISODE_SCHEMA_PATH,
    CLAIM_TARGET_AUTHORITY_SCHEMA_PATH,
    CLAIM_TARGET_AUTHORITY_PATH,
    POLICY_SCHEMA_PATH,
    GITHUB_ADMISSION_REQUEST_SCHEMA_PATH,
    GITHUB_PROVIDER_STATE_SCHEMA_PATH,
    TRUST_SCHEMA_PATH,
    REVIEW_PACKAGE_SCHEMA_PATH,
    VIEW_SCHEMA_PATH,
    RESULT_SCHEMA_PATH,
    RUNNER_ARTIFACT_INDEX_SCHEMA_PATH,
    CASE_FILE_AUTHORITY_SCHEMA_PATH,
    CASE_FILE_AUTHORITIES_PATH,
    CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH,
    REAL_SNAPSHOT_MATERIALIZATION_SCHEMA_PATH,
    *REAL_SNAPSHOT_MATERIALIZATION_PATHS,
    *CONTROLLED_BUSINESS_FIXTURE_PATHS,
    GRADER_RUBRIC_PATH,
    *AUTHORITY_PACKAGE_SCHEMA_PATHS,
    *AUTHORITY_PACKAGE_INSTANCE_PATHS,
    AUTHORITY_PACKAGE_VALIDATOR_PATH,
    *TRUST_ARTIFACT_PATHS,
    REVIEW_PACKAGES_PATH,
    *VERIFIER_CODE_PATHS,
    *GITHUB_AUTHORITY_PATHS,
)

VERIFIER_RELEASE_PATHS = (
    ROOT / ".python-version",
    ROOT / "package.json",
    ROOT / "package-lock.json",
    ROOT / "pyproject.toml",
    ROOT / "uv.lock",
    ROOT / "tools" / "isolation-policy.json",
    MISSING_CONTRACT_BACKLOG_PATH,
    POLICY_SCHEMA_PATH,
    CLAIM_TARGET_AUTHORITY_SCHEMA_PATH,
    CLAIM_TARGET_AUTHORITY_PATH,
    GITHUB_ADMISSION_REQUEST_SCHEMA_PATH,
    GITHUB_PROVIDER_STATE_SCHEMA_PATH,
    TRUST_SCHEMA_PATH,
    REVIEW_PACKAGE_SCHEMA_PATH,
    VIEW_SCHEMA_PATH,
    RESULT_SCHEMA_PATH,
    RUNNER_ARTIFACT_INDEX_SCHEMA_PATH,
    CASE_FILE_AUTHORITY_SCHEMA_PATH,
    CASE_FILE_AUTHORITIES_PATH,
    CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH,
    REAL_SNAPSHOT_MATERIALIZATION_SCHEMA_PATH,
    *AUTHORITY_PACKAGE_SCHEMA_PATHS,
    AUTHORITY_PACKAGE_VALIDATOR_PATH,
    *VERIFIER_CODE_PATHS,
    *GITHUB_AUTHORITY_PATHS,
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_strict(path: Path) -> Any:
    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError("duplicate JSON key: {}".format(key))
            value[key] = child
        return value

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
    )


def _validate_authority_package() -> tuple[dict[str, int] | None, list[str]]:
    try:
        spec = importlib.util.spec_from_file_location(
            "gate3_authority_package_validator",
            AUTHORITY_PACKAGE_VALIDATOR_PATH,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("validator module cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.validate_package(), []
    except Exception as error:
        return None, ["authority observation package invalid: {}".format(error)]


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


def case_file_authority_content_core(
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the immutable authority content covered by independent reviews."""

    return {
        key: value
        for key, value in authority.items()
        if key not in {"status", "content_sha256", "review_records"}
    }


def expected_run_variant_keys(
    episode_ids: set[str],
    episodes_by_id: Mapping[str, Mapping[str, Any]],
) -> set[tuple[str, str, str]]:
    return {
        (episode_id, kind, sibling_id)
        for episode_id in episode_ids
        for kind, sibling_id in [
            ("base", ""),
            *[
                ("counterfactual", sibling["sibling_id"])
                for sibling in episodes_by_id[episode_id][
                    "counterfactual_siblings"
                ]
            ],
        ]
    }


def _case_file_readiness_gaps(
    catalog: Mapping[str, Any],
    case_file_authorities: Mapping[str, Any],
    *,
    workspace_root: Path = WORKSPACE_ROOT,
    authorized_review_sha256s: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    authorized_review_sha256s = authorized_review_sha256s or set()
    authority_records = case_file_authorities["authorities"]
    authority_ids = [
        authority["authority_id"] for authority in authority_records
    ]
    authorities_by_id = {
        authority["authority_id"]: authority
        for authority in authority_records
    }
    completed_binding_entries = [
        (episode["episode_id"], binding)
        for episode in catalog["episodes"]
        if episode["support_expectation"]["authoring_status"]
        == "claim_cases_complete"
        for binding in episode["data_source_bindings"]
        if binding["source_mode"] != "known_contract_gap"
    ]
    completed_variant_bindings: list[
        tuple[str, list[Mapping[str, Any]]]
    ] = []
    for episode in catalog["episodes"]:
        if (
            episode["support_expectation"]["authoring_status"]
            != "claim_cases_complete"
        ):
            continue
        completed_variant_bindings.append(
            (
                episode["episode_id"],
                [
                    binding
                    for binding in episode["data_source_bindings"]
                    if binding["source_mode"] != "known_contract_gap"
                ],
            )
        )
        base_bindings = {
            binding["binding_id"]: binding
            for binding in episode["data_source_bindings"]
        }
        for sibling in episode["counterfactual_siblings"]:
            if (
                sibling["mutation_operation"]["execution_status"]
                != "executable_verified"
            ):
                continue
            materialized = materialize_counterfactual_episode(
                episode, sibling
            )
            case_ref = "{}:{}".format(
                episode["episode_id"],
                sibling["sibling_id"],
            )
            completed_variant_bindings.append(
                (
                    case_ref,
                    [
                        binding
                        for binding in materialized[
                            "data_source_bindings"
                        ]
                        if binding["source_mode"] != "known_contract_gap"
                    ],
                )
            )
            for binding in materialized["data_source_bindings"]:
                if (
                    binding["source_mode"] != "known_contract_gap"
                    and base_bindings.get(binding["binding_id"])
                    != binding
                ):
                    completed_binding_entries.append(
                        (
                            case_ref,
                            binding,
                        )
                    )
    completed_bindings = [
        binding for _, binding in completed_binding_entries
    ]
    used_authority_ids = {
        binding["authority_ref"].split("#", 1)[1]
        for binding in completed_bindings
        if "#" in binding["authority_ref"]
    }
    missing_authority_ids = sorted(
        used_authority_ids - set(authorities_by_id)
    )
    pending_authority_ids = sorted(
        authority_id
        for authority_id in used_authority_ids
        if authority_id in authorities_by_id
        and authorities_by_id[authority_id]["status"]
        != "independently_reviewed"
    )
    pending_materializations = sorted(
        "{}:{}".format(case_ref, binding["binding_id"])
        for case_ref, binding in completed_binding_entries
        if binding["materialization_status"] != "verified"
    )
    integrity_gaps = [
        "duplicate authority id {}".format(authority_id)
        for authority_id, count in Counter(authority_ids).items()
        if count > 1
    ]
    for case_ref, bindings in completed_variant_bindings:
        authority_ids_by_slot: dict[str, set[str]] = {}
        for binding in bindings:
            authority_ref = binding["authority_ref"]
            if "#" not in authority_ref:
                continue
            authority_id = authority_ref.split("#", 1)[1]
            authority = authorities_by_id.get(authority_id)
            if authority is None:
                continue
            authority_ids_by_slot.setdefault(
                authority["authority_slot_id"], set()
            ).add(authority_id)
        for slot_id, slot_authority_ids in sorted(
            authority_ids_by_slot.items()
        ):
            if len(slot_authority_ids) > 1:
                integrity_gaps.append(
                    "{} exposes conflicting authorities {} for slot {}".format(
                        case_ref,
                        sorted(slot_authority_ids),
                        slot_id,
                    )
                )
    completed_bindings_by_authority: dict[str, list[Mapping[str, Any]]] = {}
    for binding in completed_bindings:
        authority_ref = binding["authority_ref"]
        if "#" not in authority_ref:
            continue
        authority_id = authority_ref.split("#", 1)[1]
        completed_bindings_by_authority.setdefault(authority_id, []).append(
            binding
        )
    for authority_id in sorted(used_authority_ids & set(authorities_by_id)):
        authority = authorities_by_id[authority_id]
        independently_reviewed = (
            authority["status"] == "independently_reviewed"
        )
        materializations = authority.get("materializations", [])
        materialization_refs = [
            item.get("source_ref") for item in materializations
        ]
        duplicate_materialization_refs = [
            source_ref
            for source_ref, count in Counter(materialization_refs).items()
            if count > 1
        ]
        if duplicate_materialization_refs:
            integrity_gaps.append(
                "{} duplicates materializations {}".format(
                    authority_id, sorted(duplicate_materialization_refs)
                )
            )
        materializations_by_ref = {
            item["source_ref"]: item
            for item in materializations
            if isinstance(item, Mapping) and "source_ref" in item
        }
        for binding in completed_bindings_by_authority.get(
            authority_id, []
        ):
            materialization = materializations_by_ref.get(
                binding["source_ref"]
            )
            if materialization is None:
                if (
                    independently_reviewed
                    or binding["materialization_status"] == "verified"
                ):
                    integrity_gaps.append(
                        "{} lacks materialization for {}".format(
                            authority_id, binding["source_ref"]
                        )
                    )
                continue
            identity_values = materialization.get("identity_values", {})
            artifact_path = _resolve_workspace_ref(
                workspace_root,
                materialization.get("artifact_ref", ""),
            )
            if artifact_path is None or not artifact_path.is_file():
                integrity_gaps.append(
                    "{} materialization {} has no resolvable artifact".format(
                        authority_id, binding["source_ref"]
                    )
                )
            else:
                artifact_document: Mapping[str, Any] | None = None
                if artifact_path.suffix == ".json":
                    try:
                        loaded_artifact = _load_json_strict(artifact_path)
                    except (OSError, ValueError, json.JSONDecodeError):
                        integrity_gaps.append(
                            "{} materialization {} is not strict JSON".format(
                                authority_id, binding["source_ref"]
                            )
                        )
                    else:
                        if isinstance(loaded_artifact, Mapping):
                            artifact_document = loaded_artifact
                actual_artifact_sha256 = _evaluated_file_sha256(
                    artifact_path
                )
                if (
                    materialization.get("artifact_content_sha256")
                    != actual_artifact_sha256
                ):
                    integrity_gaps.append(
                        "{} materialization {} artifact hash is stale".format(
                            authority_id, binding["source_ref"]
                        )
                    )
                if (
                    binding["source_mode"]
                    == "controlled_synthetic_fixture"
                    and identity_values.get("fixture_content_sha256")
                    != actual_artifact_sha256
                ):
                    integrity_gaps.append(
                        "{} fixture {} identity hash disagrees with artifact".format(
                            authority_id, binding["source_ref"]
                        )
                    )
                if artifact_document is None:
                    integrity_gaps.append(
                        "{} materialization {} has no recognized JSON "
                        "object contract".format(
                            authority_id, binding["source_ref"]
                        )
                    )
                else:
                    artifact_type = artifact_document.get("artifact_type")
                    artifact_version = artifact_document.get(
                        "artifact_version"
                    )
                    is_real_materialization = (
                        artifact_type
                        == "gate3_real_snapshot_materialization"
                    )
                    is_controlled_fixture = (
                        artifact_type
                        == "gate3_controlled_business_fixture"
                    )
                    is_prior_authority_fixture = (
                        artifact_version
                        == "gate3.prior-authority-fixture.v1"
                    )
                    if (
                        binding["source_mode"] == "frozen_real_snapshot"
                        and not is_real_materialization
                    ):
                        integrity_gaps.append(
                            "{} real materialization {} has an unrecognized "
                            "artifact contract".format(
                                authority_id, binding["source_ref"]
                            )
                        )
                    if (
                        binding["source_mode"]
                        == "controlled_synthetic_fixture"
                        and not (
                            is_controlled_fixture
                            or is_prior_authority_fixture
                        )
                    ):
                        integrity_gaps.append(
                            "{} fixture {} has an unrecognized artifact "
                            "contract".format(
                                authority_id, binding["source_ref"]
                            )
                        )
                    if is_real_materialization:
                        if (
                            artifact_document.get("authority_id")
                            != authority_id
                            or artifact_document.get("evaluation_clock")
                            != authority["evaluation_clock"]
                        ):
                            integrity_gaps.append(
                                "{} real materialization authority or clock drifts".format(
                                    authority_id
                                )
                            )
                        source_records = [
                            source
                            for source in artifact_document.get(
                                "sources", []
                            )
                            if isinstance(source, Mapping)
                            and source.get("source_ref")
                            == binding["source_ref"]
                        ]
                        if len(source_records) != 1:
                            integrity_gaps.append(
                                "{} real materialization does not uniquely bind {}".format(
                                    authority_id, binding["source_ref"]
                                )
                            )
                        else:
                            source_record = source_records[0]
                            for identity_field in (
                                "snapshot_release_ref",
                                "coverage_watermark_ref",
                                "query_result_ref",
                            ):
                                if (
                                    identity_field in identity_values
                                    and identity_values[identity_field]
                                    != source_record.get(identity_field)
                                ):
                                    integrity_gaps.append(
                                        "{} real materialization {} identity {} drifts".format(
                                            authority_id,
                                            binding["source_ref"],
                                            identity_field,
                                        )
                                    )
                    elif is_controlled_fixture:
                        if (
                            artifact_document.get("fixture_id")
                            != authority_id
                            or artifact_document.get("fixture_version_ref")
                            != identity_values.get("fixture_version_ref")
                        ):
                            integrity_gaps.append(
                                "{} fixture authority identity drifts".format(
                                    authority_id
                                )
                            )
                    elif is_prior_authority_fixture:
                        if (
                            artifact_document.get("source_authority_id")
                            != authority_id
                            or artifact_document.get("fixture_uri")
                            != identity_values.get("fixture_version_ref")
                        ):
                            integrity_gaps.append(
                                "{} prior-authority fixture identity drifts".format(
                                    authority_id
                                )
                            )
            missing_identity_fields = sorted(
                set(binding["required_identity_fields"])
                - set(identity_values)
            )
            if missing_identity_fields:
                integrity_gaps.append(
                    "{} materialization {} lacks identities {}".format(
                        authority_id,
                        binding["source_ref"],
                        missing_identity_fields,
                    )
                )
        if not independently_reviewed:
            continue
        expected_content_sha256 = canonical_sha256(
            case_file_authority_content_core(authority)
        )
        if authority.get("content_sha256") != expected_content_sha256:
            integrity_gaps.append(
                "{} content hash does not match authority core".format(
                    authority_id
                )
            )
        review_records = authority.get("review_records", [])
        review_ids = [record.get("review_id") for record in review_records]
        if len(review_ids) != len(set(review_ids)):
            integrity_gaps.append(
                "{} contains duplicate review ids".format(authority_id)
            )
        roles = [record.get("role") for record in review_records]
        principals = [
            record.get("principal_id") for record in review_records
        ]
        if (
            set(roles) != set(authority["required_reviews"])
            or len(roles) != len(set(roles))
        ):
            integrity_gaps.append(
                "{} lacks exactly one approval per required role".format(
                    authority_id
                )
            )
        if len(principals) != len(set(principals)):
            integrity_gaps.append(
                "{} reviews do not have independent principals".format(
                    authority_id
                )
            )
        if any(
            record.get("verdict") != "approved"
            or record.get("reviewed_content_sha256")
            != expected_content_sha256
            for record in review_records
        ):
            integrity_gaps.append(
                "{} contains stale or unapproved review records".format(
                    authority_id
                )
            )
        unauthorized_review_ids = sorted(
            record.get("review_id", "")
            for record in review_records
            if canonical_sha256(record)
            not in authorized_review_sha256s
        )
        if unauthorized_review_ids:
            integrity_gaps.append(
                "{} reviews lack protected external authorization {}".format(
                    authority_id, unauthorized_review_ids
                )
            )
    return (
        missing_authority_ids,
        pending_authority_ids,
        pending_materializations,
        sorted(integrity_gaps),
    )


def _evaluated_file_sha256(path: Path) -> str:
    if path.suffix == ".json":
        try:
            return canonical_sha256(_load_json(path))
        except (OSError, ValueError, json.JSONDecodeError):
            return hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluated_artifact_hashes() -> dict[str, str]:
    def artifact_name(path: Path) -> str:
        if path.is_relative_to(EVAL_ROOT):
            return str(path.relative_to(EVAL_ROOT))
        if path.is_relative_to(ROOT):
            return str(path.relative_to(ROOT))
        return str(path.relative_to(WORKSPACE_ROOT))

    return {
        artifact_name(path): _evaluated_file_sha256(path)
        for path in EVALUATED_PATHS
    }


def verifier_release_sha256() -> str:
    return canonical_file_set_sha256(
        VERIFIER_RELEASE_PATHS,
        relative_to=WORKSPACE_ROOT,
    )


def build_admission_expectation(
    policy: Mapping[str, Any],
) -> AdmissionExpectation:
    return AdmissionExpectation(
        policy_sha256=canonical_sha256(policy),
        authority_root_bundle_sha256=canonical_sha256(
            _authority_root_bundle(policy)
        ),
        verifier_release_sha256=verifier_release_sha256(),
        evaluated_artifact_hashes=evaluated_artifact_hashes(),
    )


def _external_admission_contract_findings(
    authority: VerifiedAdmissionAuthority,
    expected: AdmissionExpectation,
) -> list[str]:
    findings: list[str] = []
    if authority.issuer_id != "github-actions-sigstore":
        findings.append("external admission issuer is not the selected provider")
    if len(authority.authority_key_id) != 40 or any(
        character not in "0123456789abcdef"
        for character in authority.authority_key_id
    ):
        findings.append("external admission workflow revision is invalid")
    for label, value in (
        ("receipt", authority.receipt_sha256),
        ("authority state", authority.authority_state_sha256),
    ):
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            findings.append(
                "external admission {} hash is invalid".format(label)
            )
    if authority.authority_state_version < 1:
        findings.append("external admission state version is invalid")
    if authority.predecessor_receipt_sha256 is not None:
        predecessor = authority.predecessor_receipt_sha256
        if len(predecessor) != 64 or any(
            character not in "0123456789abcdef"
            for character in predecessor
        ):
            findings.append("external admission predecessor is invalid")
        if predecessor == authority.receipt_sha256:
            findings.append("external admission receipt cannot self-precede")
    if (
        authority.policy_sha256 != expected.policy_sha256
        or authority.authority_root_bundle_sha256
        != expected.authority_root_bundle_sha256
        or authority.verifier_release_sha256
        != expected.verifier_release_sha256
        or dict(authority.evaluated_artifact_hashes)
        != dict(expected.evaluated_artifact_hashes)
    ):
        findings.append(
            "external admission does not bind current repository authority"
        )
    for authorization_hash in (
        set(authority.authorized_attestation_sha256s)
        | set(authority.authorized_manifest_sha256s)
    ):
        if len(authorization_hash) != 64 or any(
            character not in "0123456789abcdef"
            for character in authorization_hash
        ):
            findings.append("external admission authorization hash is invalid")
            break
    return findings


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


def _valid_calibration_reviews(
    review_registry: Mapping[str, Any],
    reviewer_authority_roots: Mapping[str, Mapping[str, Any]],
    authorized_attestation_sha256s: set[str],
    calibration_policy: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    principals = {
        principal["principal_id"]: principal
        for principal in review_registry["principals"]
    }
    valid_records: dict[str, Mapping[str, Any]] = {}
    findings: list[str] = []
    record_ids = [
        record["calibration_review_record_id"]
        for record in review_registry["calibration_records"]
    ]
    if len(record_ids) != len(set(record_ids)):
        findings.append(
            "calibration review registry contains duplicate record ids"
        )
    for record in review_registry["calibration_records"]:
        record_id = record["calibration_review_record_id"]
        principal = principals.get(record["principal_id"])
        if (
            principal is None
            or principal["status"] != "active"
            or "calibration_reviewer" not in principal["roles"]
        ):
            findings.append(
                "{} has no active calibration reviewer".format(record_id)
            )
            continue
        if (
            calibration_policy[
                "calibration_reviewer_must_be_role_dedicated"
            ]
            and set(principal["roles"]) != {"calibration_reviewer"}
        ):
            findings.append(
                "{} reviewer is not dedicated to calibration".format(
                    record_id
                )
            )
            continue
        episode_review_principals = {
            review["principal_id"]
            for review in review_registry["records"]
            if review["episode_id"] == record["episode_id"]
            and review["decision"] == "approved"
        }
        if (
            calibration_policy[
                "calibration_reviewer_must_be_independent_from_episode_reviewers"
            ]
            and record["principal_id"] in episode_review_principals
        ):
            findings.append(
                "{} reviewer is not independent from Episode reviewers".format(
                    record_id
                )
            )
            continue
        authority_root = reviewer_authority_roots.get(
            principal["authority_root_ref"]
        )
        if (
            authority_root is None
            or authority_root["principal_id"] != principal["principal_id"]
            or "calibration_reviewer"
            not in authority_root["authorized_roles"]
        ):
            findings.append(
                "{} reviewer lacks calibration authority".format(record_id)
            )
            continue
        if canonical_sha256(record) not in authorized_attestation_sha256s:
            findings.append(
                "{} lacks protected external attestation".format(record_id)
            )
            continue
        valid_records[record_id] = record
    return valid_records, findings


def _evaluator_profile_findings(
    grader_registry: Mapping[str, Any],
    calibration_policy: Mapping[str, Any],
    *,
    workspace_root: Path = WORKSPACE_ROOT,
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    profiles = grader_registry["evaluator_profiles"]
    profiles_by_id = {
        profile["profile_id"]: profile for profile in profiles
    }
    findings: list[str] = []
    if len(profiles_by_id) != len(profiles):
        findings.append("evaluator profiles contain duplicate profile ids")
    roles = [profile["role"] for profile in profiles]
    required_roles = {
        "primary_business_analysis_agent",
        "runtime_reviewer",
        "evaluation_reviewer",
    }
    if set(roles) != required_roles or len(roles) != len(set(roles)):
        findings.append(
            "evaluator profiles must contain exactly one selected profile per role"
        )
    configurations = [
        (
            profile["provider"],
            profile["model"],
            profile["thinking"],
        )
        for profile in profiles
    ]
    if (
        calibration_policy["all_selected_role_profiles_must_be_distinct"]
        and len(configurations) != len(set(configurations))
    ):
        findings.append(
            "selected evaluator role profiles do not use distinct configurations"
        )

    rubric_cache: dict[Path, Mapping[str, Any]] = {}
    for profile in profiles:
        profile_id = profile["profile_id"]
        rubric_path = _resolve_workspace_ref(
            workspace_root, profile["rubric_ref"]
        )
        prompt_bundle_path = _resolve_workspace_ref(
            workspace_root, profile["prompt_bundle_ref"]
        )
        runner_path = _resolve_workspace_ref(
            workspace_root, profile["runner_ref"]
        )
        if (
            rubric_path is None
            or prompt_bundle_path is None
            or rubric_path != prompt_bundle_path
            or not rubric_path.is_file()
        ):
            findings.append(
                "{} prompt/rubric bundle is missing or unsafe".format(
                    profile_id
                )
            )
            continue
        if runner_path is None or not runner_path.is_file():
            findings.append(
                "{} runner is missing or unsafe".format(profile_id)
            )
            continue
        try:
            rubric = rubric_cache.setdefault(
                rubric_path, _load_json_strict(rubric_path)
            )
        except (OSError, ValueError, json.JSONDecodeError):
            findings.append(
                "{} prompt/rubric bundle is unreadable".format(profile_id)
            )
            continue
        if canonical_sha256(rubric) != profile["rubric_sha256"]:
            findings.append("{} rubric hash is stale".format(profile_id))
        contract = rubric.get("role_contracts", {}).get(
            profile["prompt_contract_id"]
        )
        if not isinstance(contract, Mapping):
            findings.append(
                "{} prompt contract is missing".format(profile_id)
            )
            continue
        if contract.get("role") != profile["role"]:
            findings.append(
                "{} prompt contract has the wrong role".format(profile_id)
            )
        for schema_field in ("input_contract", "output_contract"):
            try:
                Draft202012Validator.check_schema(
                    contract.get(schema_field)
                )
            except Exception:
                findings.append(
                    "{} {} is not valid JSON Schema".format(
                        profile_id, schema_field
                    )
                )
        prompt = contract.get("system_prompt")
        if (
            not isinstance(prompt, str)
            or hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            != profile["prompt_sha256"]
        ):
            findings.append("{} prompt hash is stale".format(profile_id))
        if (
            canonical_sha256(contract.get("input_contract"))
            != profile["input_contract_sha256"]
        ):
            findings.append(
                "{} input contract hash is stale".format(profile_id)
            )
        if (
            canonical_sha256(contract.get("output_contract"))
            != profile["output_contract_sha256"]
        ):
            findings.append(
                "{} output contract hash is stale".format(profile_id)
            )
        if hashlib.sha256(runner_path.read_bytes()).hexdigest() != profile[
            "runner_sha256"
        ]:
            findings.append("{} runner hash is stale".format(profile_id))

    required_profile = profiles_by_id.get(
        calibration_policy["required_evaluator_profile_ref"]
    )
    if (
        required_profile is None
        or required_profile["role"] != "evaluation_reviewer"
    ):
        findings.append(
            "calibration policy does not resolve an evaluation reviewer profile"
        )
    return profiles_by_id, findings


def _calibration_sample_findings(
    labels: list[Mapping[str, Any]],
    calibration_policy: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    critical_count = sum(label["critical"] for label in labels)
    base_count = sum(
        label["case_variant"]["kind"] == "base" for label in labels
    )
    counterfactual_count = sum(
        label["case_variant"]["kind"] == "counterfactual"
        for label in labels
    )
    non_pass_count = sum(
        label["human_verdict"] != "pass" for label in labels
    )
    human_verdicts = {label["human_verdict"] for label in labels}
    thresholds = (
        ("episodes", len(labels), calibration_policy["minimum_episodes"]),
        (
            "critical episodes",
            critical_count,
            calibration_policy["minimum_critical_episodes"],
        ),
        (
            "noncritical episodes",
            len(labels) - critical_count,
            calibration_policy["minimum_noncritical_episodes"],
        ),
        (
            "base variants",
            base_count,
            calibration_policy["minimum_base_variants"],
        ),
        (
            "counterfactual variants",
            counterfactual_count,
            calibration_policy["minimum_counterfactual_variants"],
        ),
        (
            "human non-pass labels",
            non_pass_count,
            calibration_policy["minimum_human_non_pass_labels"],
        ),
    )
    for label, actual, minimum in thresholds:
        if actual < minimum:
            findings.append(
                "calibration has {} {}, requires {}".format(
                    actual, label, minimum
                )
            )
    missing_verdicts = sorted(
        set(calibration_policy["required_human_verdicts"])
        - human_verdicts
    )
    if missing_verdicts:
        findings.append(
            "calibration lacks human verdicts {}".format(
                missing_verdicts
            )
        )
    return findings


def _calibration_label_findings(
    labels: list[Mapping[str, Any]],
    *,
    valid_calibration_reviews: Mapping[str, Mapping[str, Any]],
    evaluator_profiles_by_id: Mapping[str, Mapping[str, Any]],
    episodes_by_id: Mapping[str, Mapping[str, Any]],
    run_manifest: Mapping[str, Any],
    grader_registry: Mapping[str, Any],
    authority_profiles: Mapping[str, Any],
    world_profiles: Mapping[str, Any],
    workspace_root: Path = WORKSPACE_ROOT,
) -> list[str]:
    findings: list[str] = []
    result_refs = [label["grader_result_ref"] for label in labels]
    artifact_index_refs = [
        label["runner_artifact_index_ref"] for label in labels
    ]
    if len(result_refs) != len(set(result_refs)):
        findings.append("calibration labels reuse grader result refs")
    if len(artifact_index_refs) != len(set(artifact_index_refs)):
        findings.append("calibration labels reuse runner artifact indexes")

    index_schema = _load_json(RUNNER_ARTIFACT_INDEX_SCHEMA_PATH)
    run_manifest_sha256 = canonical_sha256(run_manifest)
    for label in labels:
        episode_id = label["episode_id"]
        label_prefix = "calibration label {}".format(episode_id)
        review = valid_calibration_reviews.get(
            label["human_review_ref"]
        )
        if review is None:
            findings.append(
                "{} has no valid human calibration review".format(
                    label_prefix
                )
            )
        elif (
            review["episode_id"] != episode_id
            or review["episode_core_sha256"]
            != label["episode_core_sha256"]
            or review["grader_result_sha256"]
            != label["grader_result_sha256"]
            or review["human_verdict"] != label["human_verdict"]
            or review["run_cell_id"] != label["run_cell_id"]
            or review["case_variant"] != label["case_variant"]
            or review["evaluator_profile_ref"]
            != label["evaluator_profile_ref"]
            or review["evaluator_profile_sha256"]
            != label["evaluator_profile_sha256"]
        ):
            findings.append(
                "{} disagrees with its human calibration review".format(
                    label_prefix
                )
            )

        evaluator_profile = evaluator_profiles_by_id.get(
            label["evaluator_profile_ref"]
        )
        if (
            evaluator_profile is None
            or evaluator_profile["role"] != "evaluation_reviewer"
            or evaluator_profile["lifecycle_status"]
            != "calibration_eligible"
            or canonical_sha256(evaluator_profile)
            != label["evaluator_profile_sha256"]
        ):
            findings.append(
                "{} has an ineligible or stale evaluator profile".format(
                    label_prefix
                )
            )

        episode = episodes_by_id.get(episode_id)
        if episode is None:
            findings.append(
                "{} cites an unknown Episode".format(label_prefix)
            )
        else:
            expected_critical = (
                episode["decision_stakes"]["risk_level"] == "critical"
            )
            if label["critical"] != expected_critical:
                findings.append(
                    "{} has an incorrect critical flag".format(label_prefix)
                )

        result_path = _resolve_workspace_ref(
            workspace_root, label["grader_result_ref"]
        )
        index_path = _resolve_workspace_ref(
            workspace_root, label["runner_artifact_index_ref"]
        )
        if result_path is None or not result_path.is_file():
            findings.append(
                "{} grader result is missing or unsafe".format(label_prefix)
            )
            continue
        if index_path is None or not index_path.is_file():
            findings.append(
                "{} runner artifact index is missing or unsafe".format(
                    label_prefix
                )
            )
            continue
        try:
            result = _load_json_strict(result_path)
            artifact_index = _load_json_strict(index_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            findings.append(
                "{} result package is unreadable: {}".format(
                    label_prefix, error
                )
            )
            continue
        if canonical_sha256(result) != label["grader_result_sha256"]:
            findings.append(
                "{} grader result hash is stale".format(label_prefix)
            )
        if (
            canonical_sha256(artifact_index)
            != label["runner_artifact_index_sha256"]
        ):
            findings.append(
                "{} runner artifact index hash is stale".format(
                    label_prefix
                )
            )
        index_schema_findings = _schema_findings(
            artifact_index,
            index_schema,
            label="{} runner artifact index".format(label_prefix),
        )
        findings.extend(index_schema_findings)
        if index_schema_findings:
            continue
        if artifact_index["run_manifest_sha256"] != run_manifest_sha256:
            findings.append(
                "{} runner artifact index targets another run manifest".format(
                    label_prefix
                )
            )
        result_findings = validate_result(
            result,
            authority={
                "run_manifest": run_manifest,
                "grader_registry": grader_registry,
                "authority_profiles": authority_profiles,
                "world_profiles": world_profiles,
                "artifact_index": artifact_index["run_cells"],
            },
        )
        findings.extend(
            "{} invalid grader result: {}".format(label_prefix, finding)
            for finding in result_findings
        )
        if result_findings:
            continue
        if (
            result["episode_id"] != episode_id
            or result["episode_core_sha256"]
            != label["episode_core_sha256"]
            or result["derived_final_verdict"] != label["grader_verdict"]
            or result["run_cell_id"] != label["run_cell_id"]
            or result["case_variant"] != label["case_variant"]
        ):
            findings.append(
                "{} disagrees with the bound grader result".format(
                    label_prefix
                )
            )
    return findings


def _held_out_chain_findings(
    held_out_manifest: Mapping[str, Any],
    *,
    trust_schema: Mapping[str, Any],
    authorized_attestation_sha256s: set[str],
    workspace_root: Path = WORKSPACE_ROOT,
) -> tuple[list[str], set[str]]:
    findings: list[str] = []
    verified_independent_sources: set[str] = set()
    for entry in held_out_manifest["entries"]:
        opaque_id = entry["opaque_episode_id"]
        object_receipt_path = _resolve_workspace_ref(
            workspace_root, entry["object_receipt_ref"]
        )
        if (
            object_receipt_path is None
            or not object_receipt_path.is_file()
        ):
            findings.append(
                "{} object receipt is missing".format(opaque_id)
            )
        elif hashlib.sha256(
            object_receipt_path.read_bytes()
        ).hexdigest() != entry["object_receipt_sha256"]:
            findings.append(
                "{} object receipt hash is stale".format(opaque_id)
            )
        else:
            try:
                object_receipt = _load_json_strict(object_receipt_path)
            except (OSError, ValueError, json.JSONDecodeError):
                findings.append(
                    "{} object receipt is unreadable".format(opaque_id)
                )
            else:
                expected_object_receipt = {
                    "opaque_episode_id": opaque_id,
                    "encrypted_object_ref": entry[
                        "encrypted_object_ref"
                    ],
                    "ciphertext_sha256": entry["ciphertext_sha256"],
                    "promotion_ref": entry["promotion_ref"],
                    "promotion_receipt_ref": entry[
                        "promotion_receipt_ref"
                    ],
                    "promotion_receipt_sha256": entry[
                        "promotion_receipt_sha256"
                    ],
                    "access_realm": entry["access_realm"],
                    "registry_epoch": held_out_manifest[
                        "registry_epoch"
                    ],
                }
                if object_receipt != expected_object_receipt:
                    findings.append(
                        "{} object receipt binding is invalid".format(
                            opaque_id
                        )
                    )

        promotion_receipt_path = _resolve_workspace_ref(
            workspace_root, entry["promotion_receipt_ref"]
        )
        if (
            promotion_receipt_path is None
            or not promotion_receipt_path.is_file()
        ):
            findings.append(
                "{} promotion receipt is missing".format(opaque_id)
            )
            continue
        try:
            promotion_receipt = _load_json_strict(
                promotion_receipt_path
            )
        except (OSError, ValueError, json.JSONDecodeError):
            findings.append(
                "{} promotion receipt is unreadable".format(opaque_id)
            )
            continue
        receipt_schema_findings = _schema_findings(
            promotion_receipt,
            trust_schema,
            label="{} promotion receipt".format(opaque_id),
        )
        findings.extend(receipt_schema_findings)
        if receipt_schema_findings:
            continue
        receipt_sha256 = canonical_sha256(promotion_receipt)
        if receipt_sha256 != entry["promotion_receipt_sha256"]:
            findings.append(
                "{} promotion receipt hash is stale".format(opaque_id)
            )
            continue
        if receipt_sha256 not in authorized_attestation_sha256s:
            findings.append(
                "{} promotion receipt lacks protected external authorization".format(
                    opaque_id
                )
            )
        if (
            promotion_receipt["promotion_ref"]
            != entry["promotion_ref"]
            or promotion_receipt["opaque_episode_id"] != opaque_id
        ):
            findings.append(
                "{} promotion receipt targets another held-out object".format(
                    opaque_id
                )
            )
        required_attestations = {
            promotion_receipt["source_attestation_sha256"],
            promotion_receipt["business_review_attestation_sha256"],
            promotion_receipt[
                "measurement_review_attestation_sha256"
            ],
        }
        if not required_attestations <= authorized_attestation_sha256s:
            findings.append(
                "{} source or double-review attestations are not externally authorized".format(
                    opaque_id
                )
            )
        if (
            promotion_receipt["business_reviewer_principal_id"]
            == promotion_receipt[
                "measurement_reviewer_principal_id"
            ]
        ):
            findings.append(
                "{} business and measurement reviewers are not independent".format(
                    opaque_id
                )
            )
        if (
            receipt_sha256 in authorized_attestation_sha256s
            and required_attestations <= authorized_attestation_sha256s
            and promotion_receipt["business_reviewer_principal_id"]
            != promotion_receipt[
                "measurement_reviewer_principal_id"
            ]
        ):
            verified_independent_sources.add(
                promotion_receipt["independent_source_key"]
            )
    return findings, verified_independent_sources


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
    set[str],
    set[str],
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
    verified_episode_ids: set[str] = set()
    verified_independent_sources: set[str] = set()
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
            verified_episode_ids.add(episode_id)
            verified_independent_sources.add(
                source["independent_source_key"]
            )
    return (
        verified_episode_ids,
        verified_independent_sources,
        findings,
    )


def _artifact_freshness_findings() -> list[str]:
    findings: list[str] = []
    try:
        expected_artifacts = _expected_artifacts()
    except Exception as error:
        return [
            "generated artifact expectation is blocked: {}".format(error)
        ]
    for path, expected in expected_artifacts.items():
        if not path.exists():
            findings.append("missing generated artifact {}".format(path))
        elif path.read_text(encoding="utf-8") != _render(expected):
            findings.append("stale generated artifact {}".format(path))
    return findings


def _authority_roots(
    policy: Mapping[str, Any],
    *,
    workspace_root: Path = WORKSPACE_ROOT,
    external_admission: VerifiedAdmissionAuthority | None = None,
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
    if configured_root_count and external_admission is None:
        findings.append(
            "protected CI admission is absent; local authority roots are denied"
        )
        return roots, findings
    if (
        configured_root_count
        and external_admission.authority_root_bundle_sha256
        != canonical_sha256(root_bundle)
    ):
        findings.append(
            "protected CI admission does not bind the authority root bundle"
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


def _invalid_catalog_readiness(
    catalog_findings: list[str],
) -> dict[str, Any]:
    evidence = list(
        dict.fromkeys(
            [
                "authoring catalog failed structural or semantic validation",
                *catalog_findings,
            ]
        )
    )
    return {
        "artifact_type": "readiness_manifest",
        "artifact_version": "gate3.e0-readiness.v2",
        "registry_epoch": 1,
        "evaluated_artifact_hashes": evaluated_artifact_hashes(),
        "condition_verdicts": [
            _condition("authoring_catalog_valid", False, *evidence)
        ],
        "derived_status": "blocked",
        "entry_decision": "deny_g3_1",
    }


def compute_readiness(
    *,
    authoring_catalog_path: Path = AUTHORING_CATALOG_PATH,
    admission_connector: AdmissionAuthorityConnector | None = None,
) -> tuple[dict[str, Any], list[str]]:
    findings: list[str] = []
    try:
        catalog_findings, authoring_report = validate_catalog(
            authoring_catalog_path,
            require_policy_ready=False,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        catalog_findings = [
            "authoring catalog cannot be parsed: {}".format(error)
        ]
        authoring_report = {}
    findings.extend(catalog_findings)
    if catalog_findings:
        return _invalid_catalog_readiness(catalog_findings), findings
    if (
        not COVERAGE_LEDGER_PATH.exists()
        or _load_json(COVERAGE_LEDGER_PATH) != authoring_report
    ):
        findings.append("coverage ledger is missing or stale")
    catalog = _load_json(authoring_catalog_path)
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
    transfer_research = _load_json(TRANSFER_RESEARCH_PATH)
    case_file_authorities = _load_json(CASE_FILE_AUTHORITIES_PATH)
    (
        authority_package_summary,
        authority_package_findings,
    ) = _validate_authority_package()
    findings.extend(authority_package_findings)
    verified_external_admission: VerifiedAdmissionAuthority | None = None
    if admission_connector is None:
        admission_findings = [
            "canonical external admission connector is unprovisioned"
        ]
    else:
        admission_expectation = build_admission_expectation(policy)
        try:
            (
                verified_external_admission,
                admission_findings,
            ) = admission_connector.current_authority(
                admission_expectation
            )
            if verified_external_admission is not None:
                admission_findings.extend(
                    _external_admission_contract_findings(
                        verified_external_admission,
                        admission_expectation,
                    )
                )
        except Exception as error:
            admission_findings = [
                "canonical external admission connector failed closed: {}".format(
                    error
                )
            ]
            verified_external_admission = None
    external_admission_verified = (
        verified_external_admission is not None and not admission_findings
    )
    authorized_attestation_sha256s = (
        set(verified_external_admission.authorized_attestation_sha256s)
        if external_admission_verified
        else set()
    )
    authorized_manifest_sha256s = (
        set(verified_external_admission.authorized_manifest_sha256s)
        if external_admission_verified
        else set()
    )
    authority_roots, authority_root_findings = _authority_roots(
        policy,
        external_admission=(
            verified_external_admission
            if external_admission_verified
            else None
        ),
    )
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
    case_file_schema_findings = _schema_findings(
        case_file_authorities,
        _load_json(CASE_FILE_AUTHORITY_SCHEMA_PATH),
        label=CASE_FILE_AUTHORITIES_PATH.name,
    )
    findings.extend(case_file_schema_findings)
    real_snapshot_schema = _load_json(
        REAL_SNAPSHOT_MATERIALIZATION_SCHEMA_PATH
    )
    for path in REAL_SNAPSHOT_MATERIALIZATION_PATHS:
        findings.extend(
            _schema_findings(
                _load_json(path),
                real_snapshot_schema,
                label=path.name,
            )
        )
    controlled_fixture_schema = _load_json(
        CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH
    )
    for path in CONTROLLED_BUSINESS_FIXTURE_PATHS:
        findings.extend(
            _schema_findings(
                _load_json(path),
                controlled_fixture_schema,
                label=path.name,
            )
        )
    for schema_path in (
        GITHUB_ADMISSION_REQUEST_SCHEMA_PATH,
        GITHUB_PROVIDER_STATE_SCHEMA_PATH,
        VIEW_SCHEMA_PATH,
        RESULT_SCHEMA_PATH,
        RUNNER_ARTIFACT_INDEX_SCHEMA_PATH,
        CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH,
        REAL_SNAPSHOT_MATERIALIZATION_SCHEMA_PATH,
    ):
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
    (
        missing_case_file_authority_ids,
        pending_case_file_authority_ids,
        pending_case_file_materializations,
        case_file_integrity_gaps,
    ) = _case_file_readiness_gaps(
        catalog,
        case_file_authorities,
        authorized_review_sha256s=authorized_attestation_sha256s,
    )
    transfer_ids = {
        episode["episode_id"] for episode in transfer_research["episodes"]
    }
    gated_ids = (
        set(episodes_by_id)
        | set(corpus_entries)
        | {
            package["episode_id"]
            for package in review_packages["packages"]
        }
        | {
            entry["episode_id"]
            for entry in promotion_manifest["entries"]
        }
        | {
            cell["episode_id"] for cell in run_manifest["run_cells"]
        }
    )
    transfer_isolation_findings: list[str] = []
    if (
        transfer_research.get("non_gating") is not True
        or transfer_research.get("gate_artifact_reachability")
        != "forbidden"
    ):
        transfer_isolation_findings.append(
            "transfer research metadata does not forbid Gate reachability"
        )
    leaked_transfer_ids = sorted(transfer_ids & gated_ids)
    if leaked_transfer_ids:
        transfer_isolation_findings.append(
            "transfer IDs entered Required Gate artifacts: {}".format(
                leaked_transfer_ids
            )
        )
    findings.extend(transfer_isolation_findings)
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
        source_verified_episode_ids,
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
    (
        evaluator_profiles_by_id,
        evaluator_profile_findings,
    ) = _evaluator_profile_findings(
        grader_registry,
        policy["calibration_policy"],
    )
    (
        valid_calibration_reviews,
        calibration_review_findings,
    ) = _valid_calibration_reviews(
        review_registry,
        authority_roots["reviewer_authority_roots"],
        authorized_attestation_sha256s,
        policy["calibration_policy"],
    )
    findings.extend(calibration_review_findings)
    fully_reviewed_ids = {
        episode_id
        for episode_id, roles in approved_roles.items()
        if roles == {"business_owner", "measurement_reviewer"}
    }
    required_episode_ids = set(episodes_by_id)
    reviewed_gap = sorted(required_episode_ids - fully_reviewed_ids)
    source_episode_gap = sorted(
        required_episode_ids - source_verified_episode_ids
    )
    independent_source_gap = max(
        0,
        policy["required_suite"]["minimum_independent_sources"]
        - len(verified_independent_sources),
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
    reviewed_suite_episodes = [
        episodes_by_id[episode_id]
        for episode_id in fully_reviewed_ids
        if episode_id in episodes_by_id
    ]
    reviewed_group_counts = Counter(
        episode["suite_binding"]["coverage_group"]
        for episode in reviewed_suite_episodes
    )
    reviewed_factor_refs = {
        factor
        for episode in reviewed_suite_episodes
        for factor in episode["suite_binding"]["factor_group_refs"]
    }
    reviewed_family_refs = {
        family
        for episode in reviewed_suite_episodes
        for family in episode["suite_binding"]["question_family_refs"]
    }
    reviewed_suite_binding_gaps = {
        "coverage_group_counts": {
            group: required_count - reviewed_group_counts[group]
            for group, required_count in policy["required_suite"][
                "coverage_group_counts"
            ].items()
            if reviewed_group_counts[group] < required_count
        },
        "factor_group_refs": sorted(
            set(policy["required_suite"]["required_factor_group_refs"])
            - reviewed_factor_refs
        ),
        "question_family_refs": sorted(
            set(policy["required_suite"]["required_question_family_refs"])
            - reviewed_family_refs
        ),
    }
    reviewed_suite_binding_gaps = {
        key: value
        for key, value in reviewed_suite_binding_gaps.items()
        if value
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
            not boundary.get("claim_target_ids")
            or set(boundary["claim_target_ids"])
            - set(claim_targets_by_id)
            or any(
                not claim_ceiling_allows(
                    claim_targets_by_id[target_id][
                        "design_claim_ceiling"
                    ],
                    boundary["maximum_claim_ceiling"],
                )
                for target_id in boundary.get(
                    "claim_target_ids", []
                )
                if target_id in claim_targets_by_id
            )
            for boundary in episode["support_expectation"][
                "boundary_cases"
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
            or episode["support_expectation"]["authoring_status"]
            != "claim_cases_complete"
            or {
                case["claim_target_id"]
                for case in episode["support_expectation"]["claim_cases"]
            }
            != set(claim_target_ids)
        ):
            claim_structure_gaps.append(episode["episode_id"])
        for sibling in episode["counterfactual_siblings"]:
            if validate_counterfactual_materialization(
                episode, sibling
            ):
                counterfactual_execution_gaps.append(
                    sibling["sibling_id"]
                )

    grader_profile_id_values = [
        profile["profile_id"] for profile in grader_registry["profiles"]
    ]
    grader_profiles_by_id = {
        profile["profile_id"]: profile
        for profile in grader_registry["profiles"]
    }
    registered_product_predicates = {
        predicate["predicate_id"]
        for predicate in grader_registry["predicates"]
        if predicate["layer"] == "product_behavior"
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
    profile_findings: list[str] = list(evaluator_profile_findings)
    if len(grader_profile_id_values) != len(
        set(grader_profile_id_values)
    ):
        profile_findings.append("grader registry contains duplicate profile ids")
    for entry in corpus["entries"]:
        product_profile = grader_profiles_by_id.get(
            entry["product_grader_profile_ref"]
        )
        if product_profile is None:
            profile_findings.append(
                "{} product grader profile is missing".format(
                    entry["episode_id"]
                )
            )
        elif (
            product_profile["layer"] != "product_behavior"
            or set(product_profile["required_predicate_ids"])
            - registered_product_predicates
        ):
            profile_findings.append(
                "{} product grader profile has invalid predicate authority".format(
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
        and promoted_ids == required_episode_ids
        and promoted_ids == fully_reviewed_ids
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
    calibration_label_findings = _calibration_label_findings(
        calibration_labels,
        valid_calibration_reviews=valid_calibration_reviews,
        evaluator_profiles_by_id=evaluator_profiles_by_id,
        episodes_by_id=episodes_by_id,
        run_manifest=run_manifest,
        grader_registry=grader_registry,
        authority_profiles=authority_profiles,
        world_profiles=world_profiles,
    )
    findings.extend(calibration_label_findings)
    calibration_policy = policy["calibration_policy"]
    calibration_profile = evaluator_profiles_by_id.get(
        calibration["evaluator_profile_ref"]
    )
    calibration_profile_is_eligible = (
        calibration_profile is not None
        and calibration_profile["role"] == "evaluation_reviewer"
        and calibration_profile["lifecycle_status"]
        == "calibration_eligible"
        and calibration["evaluator_profile_ref"]
        == calibration_policy["required_evaluator_profile_ref"]
        and calibration["evaluator_profile_sha256"]
        == canonical_sha256(calibration_profile)
    )
    calibration_sample_findings = _calibration_sample_findings(
        calibration_labels, calibration_policy
    )
    calibration_ready = (
        calibration["status"] == "calibrated"
        and len(calibration_label_ids) == len(set(calibration_label_ids))
        and set(calibration["labeled_episode_refs"])
        == set(calibration_label_ids)
        and not calibration_review_findings
        and not calibration_label_findings
        and not calibration_sample_findings
        and calibration["critical_false_passes"]
        == recomputed_critical_false_passes
        == calibration_policy["critical_false_passes_allowed"]
        and calibration["grader_registry_sha256"] == current_grader_hash
        and calibration_profile_is_eligible
        and set(calibration["labeled_episode_refs"])
        <= calibration_episode_ids
        and len(calibration["labeled_episode_refs"])
        >= calibration_policy["minimum_episodes"]
        and calibration["agreement"] is not None
        and recomputed_agreement is not None
        and abs(calibration["agreement"] - recomputed_agreement) < 1e-12
        and calibration["agreement"] * 10_000
        >= calibration_policy[
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
    held_out_promotion_receipt_refs = [
        entry["promotion_receipt_ref"]
        for entry in held_out_manifest["entries"]
    ]
    (
        held_out_receipt_findings,
        held_out_independent_sources,
    ) = _held_out_chain_findings(
        held_out_manifest,
        trust_schema=trust_schema,
        authorized_attestation_sha256s=(
            authorized_attestation_sha256s
        ),
    )
    findings.extend(held_out_receipt_findings)
    held_out_refs_are_external = all(
        "://" in entry["encrypted_object_ref"]
        and not entry["encrypted_object_ref"].startswith("file://")
        for entry in held_out_manifest["entries"]
    )
    held_out_policy = policy["held_out_policy"]
    held_out_ready = (
        held_out_manifest["status"] == "sealed"
        and len(held_out_manifest["entries"])
        >= held_out_policy["minimum_episodes"]
        and len(held_out_ids) == len(set(held_out_ids))
        and len(held_out_promotion_refs)
        == len(set(held_out_promotion_refs))
        and len(held_out_promotion_receipt_refs)
        == len(set(held_out_promotion_receipt_refs))
        and len(held_out_independent_sources)
        >= held_out_policy["minimum_independent_sources"]
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
    run_cell_variant_keys: set[tuple[str, str, str]] = set()
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
        variant = cell["case_variant"]
        variant_key = (
            episode_id,
            variant["kind"],
            variant.get("sibling_id", ""),
        )
        if variant_key in run_cell_variant_keys:
            run_cell_findings.append(
                "{} duplicates run variant {}".format(
                    cell_id, variant_key
                )
            )
        run_cell_variant_keys.add(variant_key)
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
        episode = episodes_by_id[episode_id]
        if variant["kind"] == "counterfactual":
            sibling = next(
                (
                    item
                    for item in episode["counterfactual_siblings"]
                    if item["sibling_id"] == variant["sibling_id"]
                ),
                None,
            )
            if (
                sibling is None
                or sibling["mutation_operation"].get(
                    "execution_status"
                )
                != "executable_verified"
                or variant["materialized_sibling_sha256"]
                != sibling["mutation_operation"].get(
                    "materialized_sibling_sha256"
                )
            ):
                run_cell_findings.append(
                    "{} has invalid counterfactual binding".format(
                        cell_id
                    )
                )
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
        and run_cell_variant_keys
        == expected_run_variant_keys(promoted_ids, episodes_by_id)
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
            (
                "protected issuer: {} / key: {} / envelope: {}".format(
                    verified_external_admission.issuer_id,
                    verified_external_admission.authority_key_id,
                    verified_external_admission.receipt_sha256,
                )
                if external_admission_verified
                else "; ".join(admission_findings)
            ),
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
            "transfer_research_isolated",
            not transfer_isolation_findings,
            "4 cross-domain probes remain outside Required catalog, review, promotion and run artifacts",
        ),
        _condition(
            "agent_evaluator_projection_contract_safe",
            not view_findings,
            "36 Required projections validate; CLI cannot emit oracle, suite binding or caller-selected future turns; runtime process isolation remains G3.2",
        ),
        _condition(
            "claim_case_files_ready",
            not case_file_schema_findings
            and not missing_case_file_authority_ids
            and not pending_case_file_authority_ids
            and not pending_case_file_materializations
            and not case_file_integrity_gaps,
            "schema findings: {}".format(
                len(case_file_schema_findings)
            ),
            "missing authorities: {}".format(
                missing_case_file_authority_ids
            ),
            "pending independent review: {}".format(
                pending_case_file_authority_ids
            ),
            "pending materializations: {}".format(
                len(pending_case_file_materializations)
            ),
            "authority integrity gaps: {}".format(
                case_file_integrity_gaps
            ),
        ),
        _condition(
            "authority_observation_package_valid",
            not authority_package_findings,
            (
                "USER008 repair package: {} milestones, {} observations, {} claim dispositions".format(
                    authority_package_summary["milestones"],
                    authority_package_summary["observations"],
                    authority_package_summary["claim_dispositions"],
                )
                if authority_package_summary is not None
                else "; ".join(authority_package_findings)
            ),
        ),
        _condition(
            "source_registry_verified",
            not source_binding_findings
            and not source_episode_gap
            and independent_source_gap == 0,
            "source binding findings: {}".format(len(source_binding_findings)),
            "Required Episode source gaps: {}".format(source_episode_gap),
            "global independent source gap: {}".format(
                independent_source_gap
            ),
            "verified independent sources: {}".format(
                len(verified_independent_sources)
            ),
        ),
        _condition(
            "independent_reviews_complete",
            not review_findings and reviewed_gap == 0,
            "{} independently double-reviewed Episodes".format(
                len(fully_reviewed_ids)
            ),
            "Required review gaps: {}".format(reviewed_gap),
        ),
        _condition(
            "reviewed_coverage_complete",
            not reviewed_coverage_gaps
            and not reviewed_suite_binding_gaps,
            "reviewed coverage gaps: {}".format(reviewed_coverage_gaps),
            "reviewed WAJEgame suite binding gaps: {}".format(
                reviewed_suite_binding_gaps
            ),
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
            "calibration review findings: {}".format(
                len(calibration_review_findings)
            ),
            "calibration label findings: {}".format(
                len(calibration_label_findings)
            ),
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
    readiness = {
        "artifact_type": "readiness_manifest",
        "artifact_version": "gate3.e0-readiness.v2",
        "registry_epoch": 1,
        "evaluated_artifact_hashes": evaluated_artifact_hashes(),
        "condition_verdicts": conditions,
        "derived_status": derived_status,
        "entry_decision": (
            "allow_g3_1" if derived_status == "ready" else "deny_g3_1"
        ),
    }
    findings.extend(profile_findings)
    return readiness, findings


def main(
    admission_connector: AdmissionAuthorityConnector | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-manifest", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--require-ready", action="store_true")
    arguments = parser.parse_args()

    readiness, findings = compute_readiness(
        admission_connector=admission_connector
    )
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
