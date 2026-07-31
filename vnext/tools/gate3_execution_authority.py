#!/usr/bin/env python3
"""Validate Gate 3 execution authority and derive strict suite results."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

try:
    from tools.compile_gate3_eval_views import compile_views
    from tools.validate_gate3_eval_catalog import (
        counterfactual_materialization_core,
        materialize_counterfactual_episode,
    )
except ModuleNotFoundError:  # direct execution from vnext/tools
    from compile_gate3_eval_views import compile_views
    from validate_gate3_eval_catalog import (
        counterfactual_materialization_core,
        materialize_counterfactual_episode,
    )


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
MANIFEST_SCHEMA_PATH = EVAL_ROOT / "gate3-execution-manifest.schema.json"
CELL_RESULT_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-execution-cell-result.schema.json"
)
MODEL_INVOCATION_SCHEMA_PATH = EVAL_ROOT / "gate3-model-invocation.schema.json"
HARD_CHECK_RESULT_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-hard-check-result.schema.json"
)
SUITE_RESULT_SCHEMA_PATH = EVAL_ROOT / "gate3-suite-result.schema.json"
TRACE_SCHEMA_PATH = EVAL_ROOT / "gate3-trace-bundle.schema.json"
TRACE_ARTIFACT_INDEX_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-trace-artifact-index.schema.json"
)
ATTEMPT_SCHEMA_PATH = (
    EVAL_ROOT / "gate3-execution-attempt-journal.schema.json"
)
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
TAXONOMY_PATH = EVAL_ROOT / "taxonomy" / "coverage-taxonomy.json"
CATALOG_PATH = EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
GRADER_REGISTRY_PATH = EVAL_ROOT / "registries" / "grader-registry.json"
CORPUS_REGISTRY_PATH = EVAL_ROOT / "registries" / "corpus-registry.json"
TRACE_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "execution-trace-profiles.json"
)
ATTEMPT_POLICY_PATH = (
    EVAL_ROOT / "profiles" / "execution-attempt-policy.json"
)
MUTATION_OPERATOR_REGISTRY_PATH = (
    EVAL_ROOT / "registries" / "mutation-operator-registry.json"
)
RELATION_RESULT_SCHEMA_PATH = EVAL_ROOT / "gate3-relation-result.schema.json"
SOURCE_RUN_MANIFEST_PATH = EVAL_ROOT / "manifests" / "run-manifest.json"

ROLE_NAMES = (
    "primary_business_analysis_agent",
    "runtime_reviewer",
    "evaluation_reviewer",
)
LANES = ("semantic_frame", "full_authority")
CLAIM_TARGET_KINDS = (
    "definition",
    "data_quality_state",
    "point_quantity",
    "distribution",
    "temporal_pattern",
    "contrast",
    "composition",
    "accounting_decomposition",
    "cohort_outcome",
    "funnel_transition",
    "association",
    "causal_effect",
    "diagnostic_set",
)
RUNNER_RELEASE_PATHS = (
    Path(__file__).resolve(),
    MANIFEST_SCHEMA_PATH,
    CELL_RESULT_SCHEMA_PATH,
    MODEL_INVOCATION_SCHEMA_PATH,
    HARD_CHECK_RESULT_SCHEMA_PATH,
    SUITE_RESULT_SCHEMA_PATH,
    TRACE_SCHEMA_PATH,
    TRACE_ARTIFACT_INDEX_SCHEMA_PATH,
    ATTEMPT_SCHEMA_PATH,
    RELATION_RESULT_SCHEMA_PATH,
    TRACE_PROFILES_PATH,
    ATTEMPT_POLICY_PATH,
    MUTATION_OPERATOR_REGISTRY_PATH,
)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def execution_runner_release_sha256() -> str:
    return canonical_sha256(
        {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in RUNNER_RELEASE_PATHS
        }
    )


def model_configuration_sha256(profile: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            "provider_ref": profile["provider"],
            "model_ref": profile["model"],
            "thinking": profile["thinking"],
            "prompt_bundle_sha256": profile["prompt_sha256"],
            "input_contract_sha256": profile["input_contract_sha256"],
            "output_contract_sha256": profile["output_contract_sha256"],
        }
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


def episode_coverage_atom_refs(episode: Mapping[str, Any]) -> list[str]:
    suite_binding = episode["suite_binding"]
    refs = {
        f"source_pool:{episode['source_pool']}",
        f"business_domain:{suite_binding['business_domain']}",
        f"coverage_group:{suite_binding['coverage_group']}",
    }
    refs.update(
        f"factor_group:{value}"
        for value in suite_binding["factor_group_refs"]
    )
    refs.update(
        f"question_family:{value}"
        for value in suite_binding["question_family_refs"]
    )
    singular = {
        "decision_goals": "decision_goal",
        "measurement_challenges": "measurement_challenge",
        "temporal_shapes": "temporal_shape",
        "data_conditions": "data_condition",
        "conversation_dynamics": "conversation_dynamic",
        "risk_types": "risk_type",
    }
    for group, prefix in singular.items():
        refs.update(
            f"{prefix}:{value}"
            for value in episode["coverage_tags"][group]
        )
    return sorted(refs)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_findings(value: Any, schema_path: Path) -> list[str]:
    schema = load_json(schema_path)
    return [
        "{}: {}".format(
            "/".join(str(part) for part in error.absolute_path) or "<root>",
            error.message,
        )
        for error in Draft202012Validator(schema).iter_errors(value)
    ]


def canonical_authority() -> dict[str, Any]:
    return {
        "policy": load_json(POLICY_PATH),
        "taxonomy": load_json(TAXONOMY_PATH),
        "catalog": load_json(CATALOG_PATH),
        "grader_registry": load_json(GRADER_REGISTRY_PATH),
        "corpus_registry": load_json(CORPUS_REGISTRY_PATH),
        "trace_profiles": load_json(TRACE_PROFILES_PATH),
        "attempt_policy": load_json(ATTEMPT_POLICY_PATH),
        "mutation_operators": load_json(MUTATION_OPERATOR_REGISTRY_PATH),
        "source_run_manifest": load_json(SOURCE_RUN_MANIFEST_PATH),
    }


def validate_execution_manifest(
    manifest: Mapping[str, Any],
    *,
    authority: Mapping[str, Any] | None = None,
) -> list[str]:
    findings = schema_findings(manifest, MANIFEST_SCHEMA_PATH)
    if findings:
        return findings
    authority = authority or canonical_authority()
    expected_hashes = {
        "source_run_manifest_sha256": canonical_sha256(
            authority["source_run_manifest"]
        ),
        "policy_sha256": canonical_sha256(authority["policy"]),
        "taxonomy_sha256": canonical_sha256(authority["taxonomy"]),
        "catalog_sha256": canonical_sha256(authority["catalog"]),
        "grader_registry_sha256": canonical_sha256(
            authority["grader_registry"]
        ),
        "trace_profiles_sha256": canonical_sha256(
            authority["trace_profiles"]
        ),
        "attempt_policy_sha256": canonical_sha256(
            authority["attempt_policy"]
        ),
        "mutation_operator_registry_sha256": canonical_sha256(
            authority["mutation_operators"]
        ),
    }
    for field, expected in expected_hashes.items():
        if manifest[field] != expected:
            findings.append(f"{field} does not bind canonical authority")
    if manifest["runner_release_sha256"] != execution_runner_release_sha256():
        findings.append("runner release does not bind executable authority")
    expected_attempt_policy = {
        key: authority["attempt_policy"][key]
        for key in (
            "maximum_attempts_per_cell",
            "terminal_selection",
            "retain_all_attempts",
            "retryable_reason_codes",
        )
    }
    if manifest["attempt_policy"] != expected_attempt_policy:
        findings.append("attempt policy differs from canonical authority")

    profiles = {
        profile["profile_id"]: profile
        for profile in authority["grader_registry"]["evaluator_profiles"]
    }
    if len(profiles) != len(
        authority["grader_registry"]["evaluator_profiles"]
    ):
        findings.append("grader registry contains duplicate evaluator profiles")
    trace_profiles = {
        profile["profile_id"]: profile
        for profile in authority["trace_profiles"]["profiles"]
    }
    if len(trace_profiles) != len(authority["trace_profiles"]["profiles"]):
        findings.append("trace profile registry contains duplicate ids")
    operators = {
        operator["operator_id"]: operator
        for operator in authority["mutation_operators"]["operators"]
    }
    if len(operators) != len(authority["mutation_operators"]["operators"]):
        findings.append("mutation operator registry contains duplicate ids")
    relation_check_profiles = {
        profile["expected_relation"]: profile
        for profile in authority["mutation_operators"][
            "relation_check_profiles"
        ]
    }
    if len(relation_check_profiles) != len(
        authority["mutation_operators"]["relation_check_profiles"]
    ):
        findings.append("mutation registry contains duplicate relation profiles")
    for operator in operators.values():
        if (
            operator["kind"] != "standalone"
            and operator["expected_relation"] not in relation_check_profiles
        ):
            findings.append(
                f"operator {operator['operator_id']} lacks a relation check profile"
            )
    episodes = {
        episode["episode_id"]: episode
        for episode in authority["catalog"]["episodes"]
    }
    corpus_entries = {
        entry["episode_id"]: entry
        for entry in authority["corpus_registry"]["entries"]
    }

    cells = manifest["cells"]
    cell_ids = [cell["execution_cell_id"] for cell in cells]
    if len(cell_ids) != len(set(cell_ids)):
        findings.append("execution_cell_id values must be unique")
    coordinates = [
        (
            cell["source_run_cell_ref"],
            cell["lane"],
            cell["wording_variant_id"],
            cell["paraphrase_index"],
            cell["repeat_index"],
            cell["seed"],
        )
        for cell in cells
    ]
    if len(coordinates) != len(set(coordinates)):
        findings.append("execution coordinates must be unique")

    for cell in cells:
        findings.extend(
            _validate_cell_source_authority(
                cell,
                episodes=episodes,
                corpus_entries=corpus_entries,
                source_run_manifest=authority["source_run_manifest"],
            )
        )
        relation = cell["relation_binding"]
        operator = operators.get(relation["operator_ref"])
        if operator is None:
            findings.append(
                f"{cell['execution_cell_id']} references an unknown operator"
            )
        else:
            if canonical_sha256(operator) != relation["operator_sha256"]:
                findings.append(
                    f"{cell['execution_cell_id']} operator hash drifted"
                )
            if operator["expected_relation"] != relation["expected_relation"]:
                findings.append(
                    f"{cell['execution_cell_id']} expected relation drifted"
                )
        trace_profile = trace_profiles.get(cell["trace_profile_ref"])
        if trace_profile is None:
            findings.append(
                f"{cell['execution_cell_id']} references an unknown trace profile"
            )
        else:
            if trace_profile["lane"] != cell["lane"]:
                findings.append(
                    f"{cell['execution_cell_id']} trace profile has wrong lane"
                )
            if canonical_sha256(trace_profile) != cell["trace_profile_sha256"]:
                findings.append(
                    f"{cell['execution_cell_id']} trace profile hash drifted"
                )
            if cell["required_stage_ids"] != trace_profile[
                "required_stage_ids"
            ]:
                findings.append(
                    f"{cell['execution_cell_id']} stage set differs from profile"
                )
        for role in ROLE_NAMES:
            binding = cell["role_profiles"][role]
            profile = profiles.get(binding["profile_ref"])
            if profile is None:
                findings.append(
                    f"{cell['execution_cell_id']} has unknown {role} profile"
                )
                continue
            if profile["role"] != role:
                findings.append(
                    f"{cell['execution_cell_id']} {role} profile has wrong role"
                )
            if canonical_sha256(profile) != binding["profile_sha256"]:
                findings.append(
                    f"{cell['execution_cell_id']} {role} profile hash drifted"
                )
        required_evaluator = authority["policy"]["calibration_policy"][
            "required_evaluator_profile_ref"
        ]
        if (
            cell["role_profiles"]["evaluation_reviewer"]["profile_ref"]
            != required_evaluator
        ):
            findings.append(
                f"{cell['execution_cell_id']} uses an unauthorized evaluator"
            )

    findings.extend(_validate_policy_floor(manifest, authority))
    findings.extend(_validate_relation_groups(manifest))
    findings.extend(_validate_full_run_universe(manifest, authority))
    return findings


def _validate_full_run_universe(
    manifest: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    if manifest["execution_scope"] == "formal" and manifest["run_mode"] != "full":
        findings.append("formal execution requires full run mode")
    if manifest["run_mode"] != "full":
        return findings
    expected_episode_ids = {
        episode["episode_id"] for episode in authority["catalog"]["episodes"]
    }
    observed_episode_ids = {
        cell["episode_id"]
        for cell in manifest["cells"]
        if cell["source_authority_kind"] != "protected_held_out"
    }
    if observed_episode_ids != expected_episode_ids:
        findings.append("full run Episode set differs from canonical catalog")
    expected_variants = {
        (episode["episode_id"], "base")
        for episode in authority["catalog"]["episodes"]
    } | {
        (episode["episode_id"], sibling["sibling_id"])
        for episode in authority["catalog"]["episodes"]
        for sibling in episode["counterfactual_siblings"]
    }
    observed_variants = {
        (
            cell["episode_id"],
            "base"
            if cell["case_variant"]["kind"] == "base"
            else cell["case_variant"].get(
                "sibling_id",
                cell["case_variant"]["kind"],
            ),
        )
        for cell in manifest["cells"]
        if cell["source_authority_kind"] != "protected_held_out"
    }
    if observed_variants != expected_variants:
        findings.append("full run case-variant set differs from canonical catalog")
    expected_operators = {
        operator["operator_id"]
        for operator in authority["mutation_operators"]["operators"]
        if operator["kind"] != "standalone"
    }
    observed_operators = {
        cell["relation_binding"]["operator_ref"]
        for cell in manifest["cells"]
        if cell["relation_binding"]["operator_ref"] != "episode_outcome"
    }
    if observed_operators != expected_operators:
        findings.append("full run operator set differs from canonical registry")
    return findings


def _validate_cell_source_authority(
    cell: Mapping[str, Any],
    *,
    episodes: Mapping[str, Mapping[str, Any]],
    corpus_entries: Mapping[str, Mapping[str, Any]],
    source_run_manifest: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    base_episode = episodes.get(cell["episode_id"])
    if base_episode is None:
        return [f"{cell['execution_cell_id']} references an unknown Episode"]
    expected_core = canonical_sha256(episode_core(base_episode))
    if cell["episode_core_sha256"] != expected_core:
        findings.append(
            f"{cell['execution_cell_id']} Episode core hash drifted"
        )
    variant = cell["case_variant"]
    episode = base_episode
    effective_core_sha256 = expected_core
    if variant["kind"] == "counterfactual":
        sibling = next(
            (
                item
                for item in base_episode["counterfactual_siblings"]
                if item["sibling_id"] == variant["sibling_id"]
            ),
            None,
        )
        if sibling is None:
            findings.append(
                f"{cell['execution_cell_id']} references an unknown sibling"
            )
        else:
            expected_sibling_sha256 = sibling["mutation_operation"][
                "materialized_sibling_sha256"
            ]
            if expected_sibling_sha256 != variant[
                "materialized_sibling_sha256"
            ]:
                findings.append(
                    f"{cell['execution_cell_id']} sibling digest drifted"
                )
            episode = materialize_counterfactual_episode(
                base_episode,
                sibling,
            )
            effective_core_sha256 = canonical_sha256(
                counterfactual_materialization_core(episode)
            )
            if effective_core_sha256 != expected_sibling_sha256:
                findings.append(
                    f"{cell['execution_cell_id']} materialized sibling drifted"
                )
    if cell["source_pool"] != episode["source_pool"]:
        findings.append(
            f"{cell['execution_cell_id']} source pool differs from Episode"
        )
    if cell["business_world_id"] != episode["business_world"]["world_id"]:
        findings.append(
            f"{cell['execution_cell_id']} business world differs from Episode"
        )
    expected_historical = episode["source_pool"] == "historical_failure"
    if cell["historical_regression"] != expected_historical:
        findings.append(
            f"{cell['execution_cell_id']} historical flag differs from Episode"
        )
    if cell["coverage_atom_refs"] != episode_coverage_atom_refs(episode):
        findings.append(
            f"{cell['execution_cell_id']} coverage atoms differ from Episode"
        )
    expected_critical = episode["decision_stakes"]["risk_level"] == "critical"
    if cell["critical"] != expected_critical:
        findings.append(
            f"{cell['execution_cell_id']} critical flag differs from Episode"
        )
    target_kinds = {
        target.get("claim_target_kind")
        for target in episode["acceptable_outcome"]["claim_targets"]
    }
    if None in target_kinds:
        findings.append(
            f"{cell['execution_cell_id']} Episode lacks typed claim target kinds"
        )
    elif set(cell["claim_target_kinds"]) != target_kinds:
        findings.append(
            f"{cell['execution_cell_id']} claim target kind set drifted"
        )
    visible_messages = [
        message["text"]
        for message in episode["user_episode"]["messages"]
        if message["turn"] <= cell["visible_turn"]
    ]
    if not visible_messages:
        findings.append(
            f"{cell['execution_cell_id']} visible turn has no user message"
        )
    else:
        base_wording_sha256 = canonical_sha256(visible_messages)
        if cell["wording_variant_id"] == "base":
            if cell["paraphrase_index"] != 0:
                findings.append(
                    f"{cell['execution_cell_id']} base wording has a paraphrase index"
                )
            if cell["wording_sha256"] != base_wording_sha256:
                findings.append(
                    f"{cell['execution_cell_id']} wording hash drifted"
                )
        else:
            findings.append(
                f"{cell['execution_cell_id']} wording variant lacks canonical paraphrase authority"
            )
    corpus_entry = corpus_entries.get(cell["episode_id"])
    if corpus_entry is None:
        findings.append(
            f"{cell['execution_cell_id']} has no corpus registry entry"
        )
    elif variant["kind"] in {"base", "counterfactual"}:
        views = compile_views(
            episode,
            {
                **corpus_entry,
                "episode_core_sha256": effective_core_sha256,
            },
            visible_turn=cell["visible_turn"],
        )
        if (
            cell["agent_world_view_sha256"]
            != views["agent_world_view"]["view_sha256"]
        ):
            findings.append(
                f"{cell['execution_cell_id']} AgentWorldView hash drifted"
            )
        if (
            cell["evaluator_oracle_view_sha256"]
            != views["evaluator_oracle_view"]["view_sha256"]
        ):
            findings.append(
                f"{cell['execution_cell_id']} EvaluatorOracleView hash drifted"
            )
    source_kind = cell["source_authority_kind"]
    if source_kind == "candidate_episode":
        if variant["kind"] == "protected_held_out":
            findings.append(
                f"{cell['execution_cell_id']} candidate source cannot be held out"
            )
        else:
            variant_ref = (
                "base"
                if variant["kind"] == "base"
                else f"sibling:{variant['sibling_id']}"
            )
            expected_ref = f"candidate:{cell['episode_id']}:{variant_ref}"
            if cell["source_run_cell_ref"] != expected_ref:
                findings.append(
                    f"{cell['execution_cell_id']} candidate source ref drifted"
                )
    elif source_kind == "frozen_run_cell":
        source_cells = {
            item["run_cell_id"]: item
            for item in source_run_manifest["run_cells"]
        }
        source_cell = source_cells.get(cell["source_run_cell_ref"])
        if source_cell is None:
            findings.append(
                f"{cell['execution_cell_id']} source run cell is absent"
            )
        else:
            expected_source_values = {
                "episode_id": cell["episode_id"],
                "episode_core_sha256": cell["episode_core_sha256"],
                "case_variant": cell["case_variant"],
            }
            for field, expected_value in expected_source_values.items():
                if source_cell[field] != expected_value:
                    findings.append(
                        f"{cell['execution_cell_id']} frozen run {field} drifted"
                    )
    elif variant["kind"] != "protected_held_out":
        findings.append(
            f"{cell['execution_cell_id']} held-out source has wrong variant"
        )
    return findings


def _validate_relation_groups(manifest: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for cell in manifest["cells"]:
        relation = cell["relation_binding"]
        groups.setdefault(relation["relation_group_id"], []).append(cell)
    for group_id, cells in groups.items():
        bindings = [cell["relation_binding"] for cell in cells]
        operator_refs = {binding["operator_ref"] for binding in bindings}
        operator_hashes = {binding["operator_sha256"] for binding in bindings}
        expected = {binding["expected_relation"] for binding in bindings}
        if len(operator_refs) != 1 or len(operator_hashes) != 1 or len(expected) != 1:
            findings.append(f"relation group {group_id} has inconsistent authority")
        roles = [binding["member_role"] for binding in bindings]
        if roles == ["singleton"]:
            if next(iter(operator_refs)) != "episode_outcome":
                findings.append(
                    f"relation group {group_id} singleton uses a relation operator"
                )
            continue
        if roles.count("anchor") != 1 or roles.count("subject") < 1:
            findings.append(
                f"relation group {group_id} requires one anchor and subjects"
            )
        if "singleton" in roles:
            findings.append(
                f"relation group {group_id} mixes singleton and relation members"
            )
        if len({cell["episode_id"] for cell in cells}) != 1:
            findings.append(
                f"relation group {group_id} crosses Episode authority"
            )
        if len({cell["lane"] for cell in cells}) != 1:
            findings.append(f"relation group {group_id} crosses execution lanes")
        operator_ref = next(iter(operator_refs))
        if operator_ref == "meaning_preserving_paraphrase":
            if len({cell["wording_sha256"] for cell in cells}) != len(cells):
                findings.append(
                    f"relation group {group_id} paraphrases are not distinct"
                )
            if len(
                {
                    canonical_sha256(cell["case_variant"])
                    for cell in cells
                }
            ) != 1:
                findings.append(
                    f"relation group {group_id} paraphrase changes case authority"
                )
        elif operator_ref in {
            "material_semantic_change",
            "boundary_or_interaction_change",
            "time_offset_change",
            "contrast_order_swap",
            "estimator_exposure_change",
            "ratio_aggregation_change",
            "cohort_horizon_change",
            "funnel_stage_order_change",
            "decomposition_residual_policy_change",
            "calendar_release_version_change",
            "evidence_identity_drift",
            "claim_scope_strength_drift",
        }:
            anchors = [
                cell
                for cell in cells
                if cell["relation_binding"]["member_role"] == "anchor"
            ]
            subjects = [
                cell
                for cell in cells
                if cell["relation_binding"]["member_role"] == "subject"
            ]
            if anchors and anchors[0]["case_variant"]["kind"] != "base":
                findings.append(
                    f"relation group {group_id} mutation anchor is not base"
                )
            if any(
                cell["case_variant"]["kind"] != "counterfactual"
                for cell in subjects
            ):
                findings.append(
                    f"relation group {group_id} mutation subject lacks a counterfactual"
                )
    return findings


def _validate_policy_floor(
    manifest: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    cells = manifest["cells"]
    critical = {
        (
            cell["episode_id"],
            canonical_sha256(cell["case_variant"]),
        )
        for cell in cells
        if cell["critical"]
    }
    run_policy = authority["policy"]["run_policy"]
    for episode_id, variant_sha256 in sorted(critical):
        group_label = f"{episode_id}/{variant_sha256}"
        semantic_cells = [
            cell
            for cell in cells
            if cell["episode_id"] == episode_id
            and canonical_sha256(cell["case_variant"]) == variant_sha256
            and cell["lane"] == "semantic_frame"
        ]
        wording_hashes = {
            cell["wording_sha256"] for cell in semantic_cells
        }
        if len(wording_hashes) < run_policy["critical_episode_paraphrases"]:
            findings.append(
                f"{group_label} has too few critical semantic paraphrases"
            )
        for wording_hash in wording_hashes:
            repeats = {
                cell["repeat_index"]
                for cell in semantic_cells
                if cell["wording_sha256"] == wording_hash
            }
            if len(repeats) < run_policy[
                "critical_episode_repeats_per_paraphrase"
            ]:
                findings.append(
                    f"{group_label}/{wording_hash} has too few semantic repeats"
                )
        authority_cells = [
            cell
            for cell in cells
            if cell["episode_id"] == episode_id
            and canonical_sha256(cell["case_variant"]) == variant_sha256
            and cell["lane"] == "full_authority"
        ]
        authority_wording_hashes = {
            cell["wording_sha256"] for cell in authority_cells
        }
        if len(authority_wording_hashes) < run_policy[
            "high_risk_full_trace_paraphrases"
        ]:
            findings.append(
                f"{group_label} has too few full-authority paraphrases"
            )
        for wording_hash in authority_wording_hashes:
            repeats = {
                cell["repeat_index"]
                for cell in authority_cells
                if cell["wording_sha256"] == wording_hash
            }
            if len(repeats) < run_policy["high_risk_full_trace_repeats"]:
                findings.append(
                    f"{group_label}/{wording_hash} has too few authority repeats"
                )

    profiles = {
        profile["profile_id"]: profile
        for profile in authority["grader_registry"]["evaluator_profiles"]
    }
    if manifest["execution_scope"] == "formal":
        if manifest["status"] != "frozen":
            findings.append("formal execution manifest must be frozen")
        if manifest["realm"] != "formal_conformance":
            findings.append("formal execution requires formal_conformance realm")
        if authority["source_run_manifest"]["status"] != "frozen":
            findings.append("formal execution requires a frozen source run manifest")
        if any(
            cell["source_authority_kind"] == "candidate_episode"
            for cell in cells
        ):
            findings.append("formal execution cannot use candidate Episode cells")
        selected_refs = {
            binding["profile_ref"]
            for cell in cells
            for binding in cell["role_profiles"].values()
        }
        for profile_ref in sorted(selected_refs):
            if profiles[profile_ref]["lifecycle_status"] != "calibrated":
                findings.append(
                    f"formal execution profile {profile_ref} is not calibrated"
                )
        held_out_ids = {
            cell["episode_id"]
            for cell in cells
            if cell["case_variant"]["kind"] == "protected_held_out"
        }
        minimum_held_out = authority["policy"]["held_out_policy"][
            "minimum_episodes"
        ]
        if len(held_out_ids) < minimum_held_out:
            findings.append("formal execution omits protected held-out cells")
    elif manifest["realm"] != "development_conformance":
        findings.append("development execution requires development_conformance realm")
    return findings


def validate_trace_bundle(
    bundle: Mapping[str, Any],
    cell: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    artifact_index: Mapping[str, Any] | None,
    authority: Mapping[str, Any] | None = None,
) -> list[str]:
    findings = schema_findings(bundle, TRACE_SCHEMA_PATH)
    if findings:
        return findings
    if bundle["execution_cell_id"] != cell["execution_cell_id"]:
        findings.append("trace bundle belongs to a different execution cell")
    if bundle["execution_manifest_sha256"] != canonical_sha256(manifest):
        findings.append("trace bundle does not bind execution manifest")
    if bundle["trace_profile_ref"] != cell["trace_profile_ref"]:
        findings.append("trace profile ref does not match execution cell")
    if bundle["trace_profile_sha256"] != cell["trace_profile_sha256"]:
        findings.append("trace profile hash does not match execution cell")
    stage_ids = [stage["stage_id"] for stage in bundle["stages"]]
    if len(stage_ids) != len(set(stage_ids)):
        findings.append("trace stage ids must be unique")
    if set(stage_ids) != set(cell["required_stage_ids"]):
        findings.append("trace stage set is incomplete or unexpected")
    known = set(stage_ids)
    graph = {
        stage["stage_id"]: set(stage["predecessor_stage_ids"])
        for stage in bundle["stages"]
    }
    authority = authority or canonical_authority()
    trace_profiles = {
        profile["profile_id"]: profile
        for profile in authority["trace_profiles"]["profiles"]
    }
    trace_profile = trace_profiles.get(cell["trace_profile_ref"])
    if trace_profile is None:
        findings.append("trace profile is absent from canonical authority")
    else:
        expected_graph = {
            stage_id: set(predecessors)
            for stage_id, predecessors in trace_profile[
                "required_predecessors"
            ].items()
        }
        if graph != expected_graph:
            findings.append("trace predecessor graph differs from profile")
    for stage_id, predecessors in graph.items():
        unknown = predecessors - known
        if unknown:
            findings.append(
                f"trace stage {stage_id} has unknown predecessors"
            )
        if stage_id in predecessors:
            findings.append(f"trace stage {stage_id} depends on itself")
    if _has_cycle(graph):
        findings.append("trace predecessor graph contains a cycle")
    if artifact_index is None:
        findings.append("trace bundle requires a verified artifact index")
        return findings
    findings.extend(
        schema_findings(
            artifact_index,
            TRACE_ARTIFACT_INDEX_SCHEMA_PATH,
        )
    )
    if findings:
        return findings
    if (
        artifact_index["execution_manifest_sha256"]
        != canonical_sha256(manifest)
    ):
        findings.append("trace artifact index does not bind execution manifest")
    records = {
        record["artifact_ref"]: record
        for record in artifact_index["records"]
    }
    if len(records) != len(artifact_index["records"]):
        findings.append("trace artifact refs must be unique")
    persisted = records.get(bundle["persisted_run_trace_manifest_ref"])
    if (
        persisted is None
        or persisted["artifact_sha256"]
        != bundle["persisted_run_trace_manifest_sha256"]
    ):
        findings.append("persisted RunTraceManifest is absent or drifted")
    for stage in bundle["stages"]:
        record = records.get(stage["artifact_ref"])
        if record is None:
            findings.append(
                f"trace stage {stage['stage_id']} artifact is not indexed"
            )
            continue
        expected = {
            "artifact_sha256": stage["artifact_sha256"],
            "journal_cursor": stage["journal_cursor"],
            "authority_snapshot_sha256": stage[
                "authority_snapshot_sha256"
            ],
            "run_id": bundle["run_id"],
            "case_id": bundle["case_id"],
            "correlation_id": bundle["correlation_id"],
        }
        for field, expected_value in expected.items():
            if record[field] != expected_value:
                findings.append(
                    f"trace stage {stage['stage_id']} {field} drifted"
                )
    stage_by_id = {stage["stage_id"]: stage for stage in bundle["stages"]}
    for stage in bundle["stages"]:
        for predecessor in stage["predecessor_stage_ids"]:
            prior = stage_by_id.get(predecessor)
            if prior is not None and prior["journal_cursor"] > stage[
                "journal_cursor"
            ]:
                findings.append(
                    f"trace stage {stage['stage_id']} precedes its cause"
                )
    return findings


def validate_attempt_journal(
    journal: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
) -> list[str]:
    findings = schema_findings(journal, ATTEMPT_SCHEMA_PATH)
    if findings:
        return findings
    manifest_hash = canonical_sha256(manifest)
    if journal["execution_manifest_sha256"] != manifest_hash:
        findings.append("attempt journal does not bind execution manifest")
    expected = {
        cell["execution_cell_id"] for cell in manifest["cells"]
    }
    cell_ids = [item["execution_cell_id"] for item in journal["cell_attempts"]]
    if len(cell_ids) != len(set(cell_ids)):
        findings.append("attempt journal contains duplicate cell entries")
    if set(cell_ids) != expected:
        findings.append("attempt journal cell set differs from manifest")
    maximum = manifest["attempt_policy"]["maximum_attempts_per_cell"]
    retryable_codes = set(
        manifest["attempt_policy"]["retryable_reason_codes"]
    )
    attempt_ids: set[str] = set()
    for item in journal["cell_attempts"]:
        attempts = item["attempts"]
        numbers = [attempt["attempt_number"] for attempt in attempts]
        if numbers != list(range(1, len(attempts) + 1)):
            findings.append(
                f"{item['execution_cell_id']} attempt numbers are not contiguous"
            )
        if len(attempts) > maximum:
            findings.append(
                f"{item['execution_cell_id']} exceeds maximum attempts"
            )
        prior_id = None
        terminal_seen = False
        for attempt in attempts:
            attempt_id = attempt["attempt_id"]
            if attempt_id in attempt_ids:
                findings.append("attempt ids must be globally unique")
            attempt_ids.add(attempt_id)
            if attempt["prior_attempt_id"] != prior_id:
                findings.append(
                    f"{item['execution_cell_id']} attempt predecessor is invalid"
                )
            if terminal_seen:
                findings.append(
                    f"{item['execution_cell_id']} continued after terminal attempt"
                )
            if attempt["disposition"] == "retryable_failure":
                if attempt["reason_code"] not in retryable_codes:
                    findings.append(
                        f"{item['execution_cell_id']} used an unauthorized retry reason"
                    )
            else:
                terminal_seen = True
            prior_id = attempt_id
        if not terminal_seen:
            findings.append(
                f"{item['execution_cell_id']} has no terminal attempt"
            )
    return findings


def terminal_attempts(
    journal: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    terminals: dict[str, Mapping[str, Any]] = {}
    for item in journal["cell_attempts"]:
        terminal = next(
            (
                attempt
                for attempt in item["attempts"]
                if attempt["disposition"] != "retryable_failure"
            ),
            None,
        )
        if terminal is not None:
            terminals[item["execution_cell_id"]] = terminal
    return terminals


def trace_artifact_set_sha256(
    artifact_index: Mapping[str, Any],
) -> str:
    """Bind an execution attempt to the complete indexed runtime artifact set."""
    return canonical_sha256(artifact_index)


def _has_cycle(graph: Mapping[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for predecessor in graph.get(node, set()):
            if predecessor in graph and visit(predecessor):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def derive_review_verdict(review: Mapping[str, Any]) -> str:
    if review["critical_failure_codes"]:
        return "fail"
    if any(
        finding["status"] != "approve"
        for finding in review["claim_findings"]
    ):
        return "fail"
    disposition = review["reviewer_disposition"]
    if disposition == "needs_review":
        return "blocked"
    if disposition == "fail":
        return "fail"
    if any(score < 2 for score in review["dimension_scores"].values()):
        return "fail"
    return "pass"


def _derive_check_verdict(verdicts: Iterable[str]) -> str:
    verdicts = set(verdicts)
    if "fail" in verdicts:
        return "fail"
    if "invalid" in verdicts:
        return "invalid"
    if "blocked" in verdicts:
        return "blocked"
    if verdicts == {"pass"}:
        return "pass"
    return "invalid"


def validate_hard_check_result(
    result: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    cell: Mapping[str, Any],
    terminal_attempt_id: str,
    trace_bundle: Mapping[str, Any],
    artifact_index_sha256: str,
    grader_registry: Mapping[str, Any],
) -> list[str]:
    findings = schema_findings(result, HARD_CHECK_RESULT_SCHEMA_PATH)
    if findings:
        return findings
    expected_bindings = {
        "execution_cell_id": cell["execution_cell_id"],
        "execution_manifest_sha256": canonical_sha256(manifest),
        "terminal_attempt_id": terminal_attempt_id,
        "trace_bundle_sha256": canonical_sha256(trace_bundle),
        "artifact_index_sha256": artifact_index_sha256,
    }
    for field, expected in expected_bindings.items():
        if result[field] != expected:
            findings.append(f"hard check result {field} drifted")

    selected_profiles = [
        profile
        for profile in grader_registry["profiles"]
        if profile["layer"] in {"authority_conformance", "implementation"}
    ]
    profiles = {
        profile["layer"]: profile
        for profile in selected_profiles
    }
    if len(profiles) != len(selected_profiles):
        findings.append("grader registry has duplicate hard-check layer profiles")
    if set(profiles) != {"authority_conformance", "implementation"}:
        findings.append("grader registry lacks a required hard-check layer profile")
    expected_check_pairs = [
        (check_id, layer)
        for layer, profile in profiles.items()
        for check_id in profile["required_predicate_ids"]
    ]
    expected_checks = {
        check_id: layer
        for check_id, layer in expected_check_pairs
    }
    if len(expected_checks) != len(expected_check_pairs):
        findings.append("grader registry repeats a hard-check id")
    observed_ids = [check["check_id"] for check in result["checks"]]
    if len(observed_ids) != len(set(observed_ids)):
        findings.append("hard check ids must be unique")
    if set(observed_ids) != set(expected_checks):
        findings.append("hard check set differs from grader registry")
    for check in result["checks"]:
        expected_layer = expected_checks.get(check["check_id"])
        if expected_layer is not None and check["layer"] != expected_layer:
            findings.append(
                f"hard check {check['check_id']} has the wrong layer"
            )

    for layer in ("authority_conformance", "implementation"):
        expected = _derive_check_verdict(
            check["verdict"]
            for check in result["checks"]
            if check["layer"] == layer
        )
        if result["derived_layer_verdicts"][layer] != expected:
            findings.append(f"hard check {layer} verdict must be {expected}")
    return findings


def validate_relation_result(
    result: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    authority: Mapping[str, Any] | None = None,
    cell_results: Iterable[Mapping[str, Any]] | None = None,
) -> list[str]:
    findings = schema_findings(result, RELATION_RESULT_SCHEMA_PATH)
    if findings:
        return findings
    if result["execution_manifest_sha256"] != canonical_sha256(manifest):
        findings.append("relation result does not bind execution manifest")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for cell in manifest["cells"]:
        binding = cell["relation_binding"]
        if binding["operator_ref"] == "episode_outcome":
            continue
        grouped.setdefault(binding["relation_group_id"], []).append(cell)
    cells = grouped.get(result["relation_group_id"])
    if cells is None:
        findings.append("relation group is absent from execution manifest")
        return findings
    binding = cells[0]["relation_binding"]
    if result["operator_ref"] != binding["operator_ref"]:
        findings.append("relation operator ref drifted")
    if result["operator_sha256"] != binding["operator_sha256"]:
        findings.append("relation operator hash drifted")
    if result["expected_relation"] != binding["expected_relation"]:
        findings.append("expected relation drifted")
    if set(result["member_cell_ids"]) != {
        cell["execution_cell_id"] for cell in cells
    }:
        findings.append("relation result member set is incomplete")
    member_result_ids = [
        item["execution_cell_id"] for item in result["member_results"]
    ]
    if len(member_result_ids) != len(set(member_result_ids)):
        findings.append("relation member result ids must be unique")
    if set(member_result_ids) != {
        cell["execution_cell_id"] for cell in cells
    }:
        findings.append("relation member result set is incomplete")
    if cell_results is not None:
        expected_result_hashes = {
            item["execution_cell_id"]: canonical_sha256(item)
            for item in cell_results
        }
        for item in result["member_results"]:
            if expected_result_hashes.get(item["execution_cell_id"]) != item[
                "cell_result_sha256"
            ]:
                findings.append(
                    f"relation member {item['execution_cell_id']} result hash drifted"
                )
    check_ids = [check["check_id"] for check in result["check_results"]]
    if len(check_ids) != len(set(check_ids)):
        findings.append("relation check ids must be unique")
    check_artifact_refs = {
        artifact_ref
        for check in result["check_results"]
        for artifact_ref in check["artifact_refs"]
    }
    if set(result["artifact_refs"]) != check_artifact_refs:
        findings.append("relation artifact refs differ from check artifacts")
    authority = authority or canonical_authority()
    profiles = {
        profile["expected_relation"]: profile
        for profile in authority["mutation_operators"][
            "relation_check_profiles"
        ]
    }
    profile = profiles.get(binding["expected_relation"])
    if profile is None:
        findings.append("relation result has no registered check profile")
    elif set(check_ids) != set(profile["required_check_ids"]):
        findings.append("relation check set differs from operator profile")
    expected = _derive_check_verdict(
        check["verdict"] for check in result["check_results"]
    )
    if result["derived_verdict"] != expected:
        findings.append(f"relation verdict must be {expected}")
    return findings


def derive_cell_final_verdict(result: Mapping[str, Any]) -> str:
    layers = result["layer_verdicts"].values()
    if result["critical_vetoes"] or "fail" in layers:
        return "fail"
    if not result["trace_complete"] or "invalid" in layers:
        return "invalid"
    if "blocked" in layers:
        return "blocked"
    if all(verdict == "pass" for verdict in layers):
        return "pass"
    return "invalid"


def validate_model_invocations(
    invocations: Iterable[Mapping[str, Any]],
    *,
    cell: Mapping[str, Any],
    terminal_attempt_id: str,
    grader_registry: Mapping[str, Any],
    trace_bundle: Mapping[str, Any],
    trace_artifact_index: Mapping[str, Any],
) -> list[str]:
    invocations = list(invocations)
    findings: list[str] = []
    profiles = {
        profile["profile_id"]: profile
        for profile in grader_registry["evaluator_profiles"]
    }
    role_binding_name = {
        "primary_business_analysis_agent": "primary_business_analysis_agent",
        "message_binding": "primary_business_analysis_agent",
        "runtime_reviewer": "runtime_reviewer",
        "evaluation_reviewer": "evaluation_reviewer",
    }
    required_roles = set(role_binding_name)
    observed_success_roles: set[str] = set()
    success_job_counts: Counter[str] = Counter()
    invocation_ids: set[str] = set()
    successful_configurations: dict[str, set[str]] = {}
    trace_records = {
        record["artifact_ref"]: record
        for record in trace_artifact_index["records"]
    }
    trace_authority_snapshots = {
        stage["authority_snapshot_sha256"]
        for stage in trace_bundle["stages"]
    }
    for invocation in invocations:
        invocation_schema_findings = schema_findings(
            invocation,
            MODEL_INVOCATION_SCHEMA_PATH,
        )
        findings.extend(invocation_schema_findings)
        if invocation_schema_findings:
            continue
        attempt_id = invocation["provider_attempt_id"]
        if attempt_id in invocation_ids:
            findings.append("model invocation attempt ids must be unique")
        invocation_ids.add(attempt_id)
        if invocation["execution_cell_id"] != cell["execution_cell_id"]:
            findings.append("model invocation belongs to another cell")
        if invocation["execution_attempt_id"] != terminal_attempt_id:
            findings.append("model invocation belongs to another execution attempt")
        if invocation["case_id"] != trace_bundle["case_id"]:
            findings.append("model invocation case differs from trace")
        if invocation["correlation_id"] != trace_bundle["correlation_id"]:
            findings.append("model invocation correlation differs from trace")
        if (
            invocation["authority_snapshot_sha256"]
            not in trace_authority_snapshots
        ):
            findings.append("model invocation authority snapshot is absent from trace")
        role = invocation["role"]
        binding_name = role_binding_name[role]
        binding = cell["role_profiles"][binding_name]
        profile = profiles.get(binding["profile_ref"])
        if profile is None:
            findings.append("model invocation references an unknown profile")
            continue
        if invocation["evaluator_profile_ref"] != profile["profile_id"]:
            findings.append(f"{role} invocation profile ref drifted")
        if invocation["evaluator_profile_sha256"] != canonical_sha256(profile):
            findings.append(f"{role} invocation profile hash drifted")
        expected = {
            "provider_ref": profile["provider"],
            "model_ref": profile["model"],
            "thinking": profile["thinking"],
            "prompt_bundle_sha256": profile["prompt_sha256"],
            "input_contract_sha256": profile["input_contract_sha256"],
            "output_contract_sha256": profile["output_contract_sha256"],
        }
        for field, expected_value in expected.items():
            if invocation[field] != expected_value:
                findings.append(f"{role} invocation {field} drifted")
        if invocation["configuration_sha256"] != model_configuration_sha256(
            profile
        ):
            findings.append(f"{role} invocation configuration identity drifted")
        if invocation["disposition"] == "succeeded":
            observed_success_roles.add(role)
            success_job_counts[invocation["logical_model_job_id"]] += 1
            successful_configurations.setdefault(role, set()).add(
                invocation["configuration_sha256"]
            )
            output_record = trace_records.get(
                invocation["typed_output_artifact_ref"]
            )
            if (
                output_record is None
                or output_record["artifact_sha256"]
                != invocation["typed_output_sha256"]
            ):
                findings.append(
                    f"{role} typed output is absent or drifted in trace artifacts"
                )
    missing_roles = required_roles - observed_success_roles
    if missing_roles:
        findings.append(
            "model invocation set lacks successful roles: "
            + ",".join(sorted(missing_roles))
        )
    duplicate_success_jobs = sorted(
        job_id for job_id, count in success_job_counts.items() if count > 1
    )
    if duplicate_success_jobs:
        findings.append(
            "model invocation set has multiple successful outputs for jobs: "
            + ",".join(duplicate_success_jobs)
        )
    primary_config = successful_configurations.get(
        "primary_business_analysis_agent"
    )
    reviewer_config = successful_configurations.get("runtime_reviewer")
    evaluator_config = successful_configurations.get("evaluation_reviewer")
    if primary_config is not None and reviewer_config == primary_config:
        findings.append("Primary and runtime Reviewer configurations are not separated")
    if evaluator_config is not None and (
        evaluator_config == primary_config or evaluator_config == reviewer_config
    ):
        findings.append("Evaluation Reviewer configuration is not separated")
    return findings


def validate_cell_result(
    result: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    attempt_journal: Mapping[str, Any] | None = None,
    trace_bundle: Mapping[str, Any] | None = None,
    trace_artifact_index: Mapping[str, Any] | None = None,
    model_invocations: Iterable[Mapping[str, Any]] | None = None,
    hard_check_result: Mapping[str, Any] | None = None,
    authority: Mapping[str, Any] | None = None,
) -> list[str]:
    findings = schema_findings(result, CELL_RESULT_SCHEMA_PATH)
    if findings:
        return findings
    manifest_hash = canonical_sha256(manifest)
    if result["execution_manifest_sha256"] != manifest_hash:
        findings.append("cell result does not bind execution manifest")
    if attempt_journal is None:
        findings.append("cell result requires a verified attempt journal")
    else:
        if result["attempt_journal_sha256"] != canonical_sha256(
            attempt_journal
        ):
            findings.append("cell result does not bind attempt journal")
        terminal = terminal_attempts(attempt_journal).get(
            result["execution_cell_id"]
        )
        if terminal is None:
            findings.append("cell result has no terminal attempt")
        elif result["terminal_attempt_id"] != terminal["attempt_id"]:
            findings.append("cell result does not use first terminal attempt")
        elif result["artifact_index_sha256"] != terminal[
            "artifact_set_sha256"
        ]:
            findings.append("cell result artifact set differs from terminal attempt")
        if terminal is not None and trace_artifact_index is not None:
            expected_artifact_set = trace_artifact_set_sha256(
                trace_artifact_index
            )
            if terminal["artifact_set_sha256"] != expected_artifact_set:
                findings.append(
                    "terminal attempt artifact set does not bind trace artifact index"
                )
            if result["artifact_index_sha256"] != expected_artifact_set:
                findings.append(
                    "cell result artifact set does not bind trace artifact index"
                )
        if terminal is not None:
            terminal_disposition = terminal["disposition"]
            implementation_verdict = result["layer_verdicts"][
                "implementation"
            ]
            if (
                terminal_disposition == "terminal_failure"
                and implementation_verdict != "fail"
            ):
                findings.append(
                    "terminal failure requires an implementation fail verdict"
                )
            if (
                terminal_disposition == "superseded"
                and implementation_verdict != "blocked"
            ):
                findings.append(
                    "superseded attempt requires an implementation blocked verdict"
                )
    cells = {
        cell["execution_cell_id"]: cell for cell in manifest["cells"]
    }
    cell = cells.get(result["execution_cell_id"])
    if cell is None:
        findings.append("cell result is absent from execution manifest")
        return findings
    authority = authority or canonical_authority()
    if trace_bundle is None:
        trace_findings = ["cell result requires a verified trace bundle"]
    else:
        trace_findings = validate_trace_bundle(
            trace_bundle,
            cell,
            manifest=manifest,
            artifact_index=trace_artifact_index,
            authority=authority,
        )
        if result["trace_bundle_sha256"] != canonical_sha256(trace_bundle):
            trace_findings.append("cell result does not bind trace bundle")
        if trace_artifact_index is None or result[
            "trace_artifact_index_sha256"
        ] != canonical_sha256(trace_artifact_index):
            trace_findings.append(
                "cell result does not bind trace artifact index"
            )
    findings.extend(trace_findings)
    expected_trace_complete = not trace_findings
    if result["trace_complete"] != expected_trace_complete:
        findings.append(
            f"trace_complete must be {str(expected_trace_complete).lower()}"
        )
    if model_invocations is None:
        findings.append("cell result requires verified model invocations")
    elif trace_bundle is None or trace_artifact_index is None:
        findings.append("model invocations require verified trace artifacts")
    else:
        model_invocations = list(model_invocations)
        findings.extend(
            validate_model_invocations(
                model_invocations,
                cell=cell,
                terminal_attempt_id=result["terminal_attempt_id"],
                grader_registry=authority["grader_registry"],
                trace_bundle=trace_bundle,
                trace_artifact_index=trace_artifact_index,
            )
        )
        if result["model_invocation_set_sha256"] != canonical_sha256(
            model_invocations
        ):
            findings.append("cell result does not bind model invocations")
        evaluation_outputs = [
            invocation["typed_output_sha256"]
            for invocation in model_invocations
            if invocation.get("role") == "evaluation_reviewer"
            and invocation.get("disposition") == "succeeded"
        ]
        if evaluation_outputs != [
            canonical_sha256(result["evaluation_review"])
        ]:
            findings.append(
                "evaluation review does not bind the successful evaluator output"
            )
    if hard_check_result is None or trace_bundle is None:
        findings.append("cell result requires a verified hard check result")
    else:
        hard_check_findings = validate_hard_check_result(
            hard_check_result,
            manifest=manifest,
            cell=cell,
            terminal_attempt_id=result["terminal_attempt_id"],
            trace_bundle=trace_bundle,
            artifact_index_sha256=result["artifact_index_sha256"],
            grader_registry=authority["grader_registry"],
        )
        findings.extend(hard_check_findings)
        if result["hard_check_result_sha256"] != canonical_sha256(
            hard_check_result
        ):
            findings.append("cell result does not bind hard check result")
        if not hard_check_findings:
            for layer in ("authority_conformance", "implementation"):
                expected_layer_verdict = hard_check_result[
                    "derived_layer_verdicts"
                ][layer]
                if result["layer_verdicts"][layer] != expected_layer_verdict:
                    findings.append(
                        f"{layer} verdict must be {expected_layer_verdict}"
                    )
    evaluation_binding = cell["role_profiles"]["evaluation_reviewer"]
    if (
        result["evaluation_review"]["reviewer_profile_ref"]
        != evaluation_binding["profile_ref"]
    ):
        findings.append("evaluation review uses the wrong reviewer profile")
    review = result["evaluation_review"]
    product_profiles = [
        profile
        for profile in authority["grader_registry"]["profiles"]
        if profile["layer"] == "product_behavior"
    ]
    if len(product_profiles) != 1:
        findings.append(
            "grader registry must contain one product behavior profile"
        )
    elif set(review["evaluated_predicate_ids"]) != set(
        product_profiles[0]["required_predicate_ids"]
    ):
        findings.append(
            "evaluation review predicate set differs from grader registry"
        )
    claim_refs = [finding["claim_ref"] for finding in review["claim_findings"]]
    if len(claim_refs) != len(set(claim_refs)):
        findings.append("evaluation review claim refs must be unique")
    for claim_finding in review["claim_findings"]:
        if (
            claim_finding["status"] == "approve"
            and claim_finding["repair_target"] != "none"
        ):
            findings.append("approved claim cannot carry a repair target")
        if (
            claim_finding["status"] != "approve"
            and claim_finding["repair_target"] == "none"
        ):
            findings.append("non-approved claim requires a repair target")
    if (
        review["reviewer_disposition"] == "needs_review"
        and review["abstention_reason"] is None
    ):
        findings.append("needs_review requires an abstention reason")
    if (
        review["reviewer_disposition"] != "needs_review"
        and review["abstention_reason"] is not None
    ):
        findings.append("non-abstaining review cannot carry abstention reason")
    expected_product = derive_review_verdict(review)
    if result["layer_verdicts"]["product_behavior"] != expected_product:
        findings.append(
            f"product behavior verdict must be {expected_product}"
        )
    expected_final = derive_cell_final_verdict(result)
    if result["derived_final_verdict"] != expected_final:
        findings.append(f"derived final verdict must be {expected_final}")
    if (
        manifest["execution_scope"] == "formal"
        and "external_execution_receipt_sha256" not in result
    ):
        findings.append("formal cell result lacks external execution receipt")
    return findings


def derive_suite_result(
    manifest: Mapping[str, Any],
    results: Iterable[Mapping[str, Any]],
    *,
    manifest_findings: Iterable[str] = (),
    attempt_journal: Mapping[str, Any] | None = None,
    relation_results: Iterable[Mapping[str, Any]] = (),
    trace_bundles: Mapping[str, Mapping[str, Any]] | None = None,
    trace_artifact_indexes: Mapping[str, Mapping[str, Any]] | None = None,
    model_invocations_by_cell: Mapping[
        str,
        Iterable[Mapping[str, Any]],
    ]
    | None = None,
    hard_check_results: Mapping[str, Mapping[str, Any]] | None = None,
    authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    results = list(results)
    authority = authority or canonical_authority()
    derived_manifest_findings = validate_execution_manifest(
        manifest,
        authority=authority,
    )
    manifest_findings = sorted(
        set(manifest_findings) | set(derived_manifest_findings)
    )
    trace_bundles = trace_bundles or {}
    trace_artifact_indexes = trace_artifact_indexes or {}
    model_invocations_by_cell = model_invocations_by_cell or {}
    hard_check_results = hard_check_results or {}
    expected = {
        cell["execution_cell_id"] for cell in manifest["cells"]
    }
    observed_ids = [result.get("execution_cell_id", "") for result in results]
    counts = Counter(observed_ids)
    observed = set(observed_ids)
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    duplicates = sorted(
        cell_id for cell_id, count in counts.items() if count > 1
    )
    attempt_findings = (
        ["attempt journal is missing"]
        if attempt_journal is None
        else validate_attempt_journal(attempt_journal, manifest=manifest)
    )
    result_findings = {
        result.get("execution_cell_id", "<missing-id>"): validate_cell_result(
            result,
            manifest=manifest,
            attempt_journal=attempt_journal,
            trace_bundle=trace_bundles.get(
                str(result.get("execution_cell_id", ""))
            ),
            trace_artifact_index=trace_artifact_indexes.get(
                str(result.get("execution_cell_id", ""))
            ),
            model_invocations=model_invocations_by_cell.get(
                str(result.get("execution_cell_id", ""))
            ),
            hard_check_result=hard_check_results.get(
                str(result.get("execution_cell_id", ""))
            ),
            authority=authority,
        )
        for result in results
        if isinstance(result, Mapping)
    }
    verdict_counts = Counter(
        result.get("derived_final_verdict", "invalid") for result in results
    )
    relation_results = list(relation_results)
    expected_relation_ids = {
        cell["relation_binding"]["relation_group_id"]
        for cell in manifest["cells"]
        if cell["relation_binding"]["operator_ref"] != "episode_outcome"
    }
    observed_relation_ids = [
        result.get("relation_group_id", "") for result in relation_results
    ]
    relation_id_counts = Counter(observed_relation_ids)
    missing_relations = sorted(
        expected_relation_ids - set(observed_relation_ids)
    )
    unexpected_relations = sorted(
        set(observed_relation_ids) - expected_relation_ids
    )
    duplicate_relations = sorted(
        relation_id
        for relation_id, count in relation_id_counts.items()
        if count > 1
    )
    relation_findings = [
        finding
        for result in relation_results
        for finding in validate_relation_result(
            result,
            manifest=manifest,
            authority=authority,
            cell_results=results,
        )
    ]
    relation_verdict_counts = Counter(
        result.get("derived_verdict", "invalid")
        for result in relation_results
    )
    invalid_structure = bool(
        list(manifest_findings)
        or attempt_findings
        or unexpected
        or duplicates
        or any(result_findings.values())
        or unexpected_relations
        or duplicate_relations
        or relation_findings
    )
    if invalid_structure or verdict_counts["invalid"]:
        local_status = "invalid"
    elif verdict_counts["fail"] or relation_verdict_counts["fail"]:
        local_status = "fail"
    elif (
        missing
        or missing_relations
        or verdict_counts["blocked"]
        or relation_verdict_counts["blocked"]
    ):
        local_status = "blocked"
    elif (
        len(results) == len(expected)
        and verdict_counts["pass"] == len(expected)
        and len(relation_results) == len(expected_relation_ids)
        and relation_verdict_counts["pass"] == len(expected_relation_ids)
    ):
        local_status = "pass"
    else:
        local_status = "invalid"

    formal_blockers: list[str] = []
    if manifest["execution_scope"] != "formal":
        formal_blockers.append("development_execution_scope")
    if manifest["status"] != "frozen":
        formal_blockers.append("execution_manifest_not_frozen")
    if local_status != "pass":
        formal_blockers.append("local_execution_not_passed")
    if any(
        "external_execution_receipt_sha256" not in result
        for result in results
    ):
        formal_blockers.append("external_execution_receipts_incomplete")
    if manifest_findings:
        formal_blockers.append("execution_manifest_invalid")
    world_counts = _claim_target_kind_world_counts(manifest)
    coverage_blockers: list[str] = []
    if manifest["run_mode"] != "full":
        coverage_blockers.append("run_mode_not_full")
    missing_world_floor = [
        kind for kind in CLAIM_TARGET_KINDS if world_counts.get(kind, 0) < 3
    ]
    if missing_world_floor:
        coverage_blockers.append("claim_target_kind_world_floor_incomplete")
    if manifest_findings:
        coverage_blockers.append("execution_manifest_invalid")
    coverage_blockers = sorted(set(coverage_blockers))
    coverage_status = "blocked" if coverage_blockers else "pass"
    if coverage_status != "pass":
        formal_blockers.append("coverage_admission_not_passed")
    formal_blockers.append("protected_execution_receipt_unverified")
    formal_blockers = sorted(set(formal_blockers))
    formal_status = (
        "eligible_for_external_admission"
        if not formal_blockers
        else "blocked"
    )
    suite = {
        "artifact_type": "gate3_suite_result",
        "artifact_version": "gate3.suite-result.v1",
        "execution_manifest_sha256": canonical_sha256(manifest),
        "run_mode": manifest["run_mode"],
        "expected_cell_count": len(expected),
        "observed_cell_count": len(results),
        "missing_cell_ids": missing,
        "duplicate_cell_ids": duplicates,
        "unexpected_cell_ids": unexpected,
        "expected_relation_count": len(expected_relation_ids),
        "observed_relation_count": len(relation_results),
        "missing_relation_group_ids": missing_relations,
        "duplicate_relation_group_ids": duplicate_relations,
        "unexpected_relation_group_ids": unexpected_relations,
        "relation_verdict_counts": {
            verdict: relation_verdict_counts[verdict]
            for verdict in ("pass", "fail", "blocked", "invalid")
        },
        "cell_verdict_counts": {
            verdict: verdict_counts[verdict]
            for verdict in ("pass", "fail", "blocked", "invalid")
        },
        "trace_complete_cell_count": sum(
            bool(result.get("trace_complete")) for result in results
        ),
        "trace_incomplete_cell_count": sum(
            not bool(result.get("trace_complete")) for result in results
        ),
        "critical_episode_count": len(
            {
                cell["episode_id"]
                for cell in manifest["cells"]
                if cell["critical"]
            }
        ),
        "historical_regression_episode_count": len(
            {
                cell["episode_id"]
                for cell in manifest["cells"]
                if cell["historical_regression"]
            }
        ),
        "critical_veto_count": sum(
            len(result.get("critical_vetoes", ())) for result in results
        ),
        "claim_target_kind_world_counts": world_counts,
        "coverage_admission_status": coverage_status,
        "coverage_blockers": coverage_blockers,
        "local_evidence_trust": "runner_self_attested",
        "local_execution_status": local_status,
        "formal_admission_status": formal_status,
        "formal_blockers": formal_blockers,
    }
    schema_errors = schema_findings(suite, SUITE_RESULT_SCHEMA_PATH)
    if schema_errors:
        raise ValueError("derived suite result violates schema: " + "; ".join(schema_errors))
    return suite


def _claim_target_kind_world_counts(
    manifest: Mapping[str, Any],
) -> dict[str, int]:
    worlds_by_kind: dict[str, set[str]] = {}
    for cell in manifest["cells"]:
        for kind in cell["claim_target_kinds"]:
            worlds_by_kind.setdefault(kind, set()).add(
                cell["business_world_id"]
            )
    return {
        kind: len(worlds)
        for kind, worlds in sorted(worlds_by_kind.items())
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_json(args.manifest)
    manifest_findings = validate_execution_manifest(manifest)
    if manifest_findings:
        for finding in manifest_findings:
            print(finding)
        return 1
    print("gate3 execution manifest valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
