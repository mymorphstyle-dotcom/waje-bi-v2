from __future__ import annotations

import copy
import unittest

from tools.compile_gate3_eval_views import compile_views
from tools.validate_gate3_eval_catalog import (
    counterfactual_materialization_core,
    materialize_counterfactual_episode,
)
from tools.gate3_execution_authority import (
    canonical_authority,
    canonical_sha256,
    derive_cell_final_verdict,
    derive_review_verdict,
    derive_suite_result,
    episode_core,
    episode_coverage_atom_refs,
    execution_runner_release_sha256,
    model_configuration_sha256,
    trace_artifact_set_sha256,
    validate_attempt_journal,
    validate_cell_result,
    validate_execution_manifest,
    validate_hard_check_result,
    validate_relation_result,
    validate_trace_bundle,
    _claim_target_kind_world_counts,
)


SHA = "a" * 64


def profile_bindings(authority):
    profiles = {
        profile["role"]: profile
        for profile in authority["grader_registry"]["evaluator_profiles"]
    }
    return {
        role: {
            "profile_ref": profile["profile_id"],
            "profile_sha256": canonical_sha256(profile),
        }
        for role, profile in profiles.items()
    }


def execution_cell(authority, *, cell_id="CELL-DEV-001", critical=False):
    trace_profile = next(
        profile
        for profile in authority["trace_profiles"]["profiles"]
        if profile["lane"] == "semantic_frame"
    )
    operator = next(
        item
        for item in authority["mutation_operators"]["operators"]
        if item["operator_id"] == "episode_outcome"
    )
    episode = next(
        item
        for item in authority["catalog"]["episodes"]
        if item["episode_id"] == "G3-USER-001"
    )
    corpus_entry = next(
        item
        for item in authority["corpus_registry"]["entries"]
        if item["episode_id"] == "G3-USER-001"
    )
    core_sha = canonical_sha256(episode_core(episode))
    views = compile_views(
        episode,
        {**corpus_entry, "episode_core_sha256": core_sha},
        visible_turn=1,
    )
    return {
        "execution_cell_id": cell_id,
        "source_authority_kind": "candidate_episode",
        "source_run_cell_ref": "candidate:G3-USER-001:base",
        "episode_id": "G3-USER-001",
        "episode_core_sha256": core_sha,
        "case_variant": {"kind": "base"},
        "lane": "semantic_frame",
        "wording_variant_id": "base",
        "wording_sha256": canonical_sha256(
            [episode["user_episode"]["messages"][0]["text"]]
        ),
        "visible_turn": 1,
        "paraphrase_index": 0,
        "repeat_index": 1,
        "seed": 731,
        "critical": critical,
        "source_pool": "real_user_language",
        "business_world_id": "WORLD-PAID-AMOUNT-CHANGE-001",
        "business_world_independence_key": episode[
            "business_world_independence_key"
        ],
        "claim_target_kinds": ["contrast", "accounting_decomposition"],
        "coverage_atom_refs": episode_coverage_atom_refs(episode),
        "historical_regression": False,
        "relation_binding": {
            "relation_group_id": f"REL-{cell_id}",
            "operator_ref": operator["operator_id"],
            "operator_sha256": canonical_sha256(operator),
            "member_role": "singleton",
            "expected_relation": operator["expected_relation"],
        },
        "agent_world_view_sha256": views["agent_world_view"]["view_sha256"],
        "evaluator_oracle_view_sha256": views[
            "evaluator_oracle_view"
        ]["view_sha256"],
        "trace_profile_ref": trace_profile["profile_id"],
        "trace_profile_sha256": canonical_sha256(trace_profile),
        "role_profiles": profile_bindings(authority),
        "required_stage_ids": trace_profile["required_stage_ids"],
    }


def counterfactual_cell(authority, *, cell_id="CELL-CF-001"):
    cell = execution_cell(authority, cell_id=cell_id)
    episode = next(
        item
        for item in authority["catalog"]["episodes"]
        if item["episode_id"] == "G3-USER-001"
    )
    sibling = episode["counterfactual_siblings"][0]
    materialized = materialize_counterfactual_episode(episode, sibling)
    materialized_sha256 = canonical_sha256(
        counterfactual_materialization_core(materialized)
    )
    corpus_entry = next(
        item
        for item in authority["corpus_registry"]["entries"]
        if item["episode_id"] == "G3-USER-001"
    )
    views = compile_views(
        materialized,
        {**corpus_entry, "episode_core_sha256": materialized_sha256},
        visible_turn=1,
    )
    cell.update(
        {
            "source_run_cell_ref": (
                f"candidate:G3-USER-001:sibling:{sibling['sibling_id']}"
            ),
            "case_variant": {
                "kind": "counterfactual",
                "sibling_id": sibling["sibling_id"],
                "materialized_sibling_sha256": materialized_sha256,
            },
            "wording_sha256": canonical_sha256(
                [materialized["user_episode"]["messages"][0]["text"]]
            ),
            "coverage_atom_refs": episode_coverage_atom_refs(materialized),
            "business_world_independence_key": materialized[
                "business_world_independence_key"
            ],
            "agent_world_view_sha256": views["agent_world_view"][
                "view_sha256"
            ],
            "evaluator_oracle_view_sha256": views[
                "evaluator_oracle_view"
            ]["view_sha256"],
        }
    )
    return cell


def execution_manifest(authority, *, cells=None):
    return {
        "artifact_type": "gate3_execution_manifest",
        "artifact_version": "gate3.execution-manifest.v1",
        "execution_scope": "development",
        "run_mode": "smoke",
        "status": "draft",
        "source_run_manifest_sha256": canonical_sha256(
            authority["source_run_manifest"]
        ),
        "policy_sha256": canonical_sha256(authority["policy"]),
        "taxonomy_sha256": canonical_sha256(authority["taxonomy"]),
        "catalog_sha256": canonical_sha256(authority["catalog"]),
        "grader_registry_sha256": canonical_sha256(
            authority["grader_registry"]
        ),
        "mutation_operator_registry_sha256": canonical_sha256(
            authority["mutation_operators"]
        ),
        "trace_profiles_sha256": canonical_sha256(
            authority["trace_profiles"]
        ),
        "attempt_policy_sha256": canonical_sha256(
            authority["attempt_policy"]
        ),
        "runner_release_sha256": execution_runner_release_sha256(),
        "realm": "development_conformance",
        "attempt_policy": {
            key: authority["attempt_policy"][key]
            for key in (
                "maximum_attempts_per_cell",
                "terminal_selection",
                "retain_all_attempts",
                "retryable_reason_codes",
            )
        },
        "cells": cells or [execution_cell(authority)],
    }


def evaluation_review(*, disposition="pass", scores=None, critical=()):
    return {
        "reviewer_profile_ref": "EVALUATOR-DEEPSEEK-FLASH-THINK-V1",
        "evaluated_predicate_ids": [
            "decision_target_preserved",
            "measurement_design_defensible",
            "support_disposition_valid",
            "ambiguity_handled_proportionally",
            "evidence_claim_proportional",
            "counterfactual_relation_preserved",
            "answer_supports_decision",
            "agent_oracle_isolated",
        ],
        "dimension_scores": scores
        or {
            "question_and_measurement": 2,
            "investigation": 2,
            "evidence_and_claims": 2,
            "authority_consistency": 2,
            "answer_value": 2,
        },
        "critical_failure_codes": list(critical),
        "reviewer_disposition": disposition,
        "claim_findings": [
            {
                "claim_ref": "claim/main",
                "status": "approve",
                "responsibility_stage": "claim",
                "repair_target": "none",
                "evidence_refs": ["artifact://evidence/1"],
            }
        ],
        "concise_reason": "The run is supported by the cited artifacts.",
        "artifact_refs": ["artifact://frame/1"],
        "confidence": 0.9,
        "abstention_reason": None,
    }


def model_invocations(authority, cell, bundle):
    profiles = {
        profile["role"]: profile
        for profile in authority["grader_registry"]["evaluator_profiles"]
    }
    role_profile = {
        "primary_business_analysis_agent": profiles[
            "primary_business_analysis_agent"
        ],
        "message_binding": profiles["primary_business_analysis_agent"],
        "runtime_reviewer": profiles["runtime_reviewer"],
        "evaluation_reviewer": profiles["evaluation_reviewer"],
    }
    role_stage = {
        "primary_business_analysis_agent": "frame_proposal",
        "message_binding": "typed_binding",
        "runtime_reviewer": "frame_review",
        "evaluation_reviewer": "evaluation_review",
    }
    stages = {stage["stage_id"]: stage for stage in bundle["stages"]}
    return [
        {
            "artifact_type": "gate3_model_invocation",
            "artifact_version": "gate3.model-invocation.v1",
            "execution_cell_id": cell["execution_cell_id"],
            "execution_attempt_id": f"ATTEMPT-{cell['execution_cell_id']}-1",
            "case_id": bundle["case_id"],
            "logical_model_job_id": f"JOB-{role}",
            "provider_attempt_id": f"PROVIDER-ATTEMPT-{role}",
            "role": role,
            "evaluator_profile_ref": profile["profile_id"],
            "evaluator_profile_sha256": canonical_sha256(profile),
            "provider_ref": profile["provider"],
            "model_ref": profile["model"],
            "thinking": profile["thinking"],
            "configuration_sha256": model_configuration_sha256(profile),
            "prompt_bundle_sha256": profile["prompt_sha256"],
            "input_contract_sha256": profile["input_contract_sha256"],
            "output_contract_sha256": profile["output_contract_sha256"],
            "request_sha256": "4" * 64,
            "provider_response_id": f"RESPONSE-{role}",
            "disposition": "succeeded",
            "typed_output_sha256": stages[role_stage[role]][
                "artifact_sha256"
            ],
            "typed_output_artifact_ref": stages[role_stage[role]][
                "artifact_ref"
            ],
            "authority_snapshot_sha256": stages[role_stage[role]][
                "authority_snapshot_sha256"
            ],
            "causation_id": "CAUSE-G36-001",
            "correlation_id": bundle["correlation_id"],
            "usage": {},
            "started_at": "2026-07-31T12:00:00Z",
            "completed_at": "2026-07-31T12:00:01Z",
        }
        for role, profile in role_profile.items()
    ]


def attempt_journal(manifest, *, artifact_set_sha256=SHA):
    return {
        "artifact_type": "gate3_execution_attempt_journal",
        "artifact_version": "gate3.execution-attempt-journal.v1",
        "execution_manifest_sha256": canonical_sha256(manifest),
        "cell_attempts": [
            {
                "execution_cell_id": cell["execution_cell_id"],
                "attempts": [
                    {
                        "attempt_id": f"ATTEMPT-{cell['execution_cell_id']}-1",
                        "attempt_number": 1,
                        "prior_attempt_id": None,
                        "disposition": "terminal_success",
                        "reason_code": "completed",
                        "artifact_set_sha256": artifact_set_sha256,
                        "recorded_at": "2026-07-31T12:00:00Z",
                    }
                ],
            }
            for cell in manifest["cells"]
        ],
    }


def cell_result(
    manifest,
    journal,
    bundle,
    artifact_index,
    invocations,
    hard_checks,
    *,
    cell_id="CELL-DEV-001",
):
    result = {
        "artifact_type": "gate3_execution_cell_result",
        "artifact_version": "gate3.execution-cell-result.v1",
        "execution_cell_id": cell_id,
        "execution_manifest_sha256": canonical_sha256(manifest),
        "layer_verdicts": {
            "product_behavior": "pass",
            "authority_conformance": "pass",
            "implementation": "pass",
        },
        "evaluation_review": evaluation_review(),
        "trace_complete": True,
        "artifact_index_sha256": journal["cell_attempts"][0]["attempts"][0][
            "artifact_set_sha256"
        ],
        "trace_bundle_sha256": canonical_sha256(bundle),
        "trace_artifact_index_sha256": canonical_sha256(artifact_index),
        "model_invocation_set_sha256": canonical_sha256(invocations),
        "hard_check_result_sha256": canonical_sha256(hard_checks),
        "attempt_journal_sha256": canonical_sha256(journal),
        "terminal_attempt_id": f"ATTEMPT-{cell_id}-1",
        "critical_vetoes": [],
        "derived_final_verdict": "pass",
    }
    return result


def hard_check_result(authority, manifest, cell, journal, bundle):
    checks = []
    for profile in authority["grader_registry"]["profiles"]:
        if profile["layer"] not in {
            "authority_conformance",
            "implementation",
        }:
            continue
        for check_id in profile["required_predicate_ids"]:
            checks.append(
                {
                    "check_id": check_id,
                    "layer": profile["layer"],
                    "verdict": "pass",
                    "observation_sha256": SHA,
                    "artifact_refs": [f"artifact://check/{check_id}"],
                    "concise_reason": "The bound observations satisfy the check.",
                }
            )
    return {
        "artifact_type": "gate3_hard_check_result",
        "artifact_version": "gate3.hard-check-result.v1",
        "execution_cell_id": cell["execution_cell_id"],
        "execution_manifest_sha256": canonical_sha256(manifest),
        "terminal_attempt_id": journal["cell_attempts"][0]["attempts"][0][
            "attempt_id"
        ],
        "trace_bundle_sha256": canonical_sha256(bundle),
        "artifact_index_sha256": journal["cell_attempts"][0]["attempts"][0][
            "artifact_set_sha256"
        ],
        "checks": checks,
        "derived_layer_verdicts": {
            "authority_conformance": "pass",
            "implementation": "pass",
        },
    }


def trace_bundle(cell, manifest):
    stages = []
    prior = None
    for index, stage_id in enumerate(cell["required_stage_ids"], start=1):
        stages.append(
            {
                "stage_id": stage_id,
                "artifact_ref": f"artifact://{stage_id}",
                "artifact_sha256": (
                    canonical_sha256(evaluation_review())
                    if stage_id == "evaluation_review"
                    else format(index, "064x")
                ),
                "journal_cursor": index,
                "authority_snapshot_sha256": format(index + 100, "064x"),
                "predecessor_stage_ids": [] if prior is None else [prior],
            }
        )
        prior = stage_id
    bundle = {
        "artifact_type": "gate3_trace_bundle",
        "artifact_version": "gate3.trace-bundle.v1",
        "execution_manifest_sha256": canonical_sha256(manifest),
        "execution_cell_id": cell["execution_cell_id"],
        "run_id": "RUN-G36-001",
        "case_id": "CASE-G36-001",
        "correlation_id": "CORR-G36-001",
        "trace_profile_ref": cell["trace_profile_ref"],
        "trace_profile_sha256": cell["trace_profile_sha256"],
        "persisted_run_trace_manifest_ref": "artifact://run-trace/1",
        "persisted_run_trace_manifest_sha256": "f" * 64,
        "stages": stages,
    }
    records = [
        {
            "artifact_ref": stage["artifact_ref"],
            "artifact_sha256": stage["artifact_sha256"],
            "run_id": bundle["run_id"],
            "case_id": bundle["case_id"],
            "correlation_id": bundle["correlation_id"],
            "journal_cursor": stage["journal_cursor"],
            "authority_snapshot_sha256": stage[
                "authority_snapshot_sha256"
            ],
        }
        for stage in stages
    ]
    records.append(
        {
            "artifact_ref": bundle["persisted_run_trace_manifest_ref"],
            "artifact_sha256": bundle[
                "persisted_run_trace_manifest_sha256"
            ],
            "run_id": bundle["run_id"],
            "case_id": bundle["case_id"],
            "correlation_id": bundle["correlation_id"],
            "journal_cursor": stages[-1]["journal_cursor"],
            "authority_snapshot_sha256": stages[-1][
                "authority_snapshot_sha256"
            ],
        }
    )
    artifact_index = {
        "artifact_type": "gate3_trace_artifact_index",
        "artifact_version": "gate3.trace-artifact-index.v1",
        "execution_manifest_sha256": canonical_sha256(manifest),
        "records": records,
    }
    return bundle, artifact_index


class Gate36ExecutionAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = copy.deepcopy(canonical_authority())
        self.manifest = execution_manifest(self.authority)

    def test_development_manifest_binds_profiles_and_trace(self) -> None:
        self.assertEqual(
            validate_execution_manifest(
                self.manifest,
                authority=self.authority,
            ),
            [],
        )
        attacked = copy.deepcopy(self.manifest)
        attacked["cells"][0]["role_profiles"][
            "evaluation_reviewer"
        ]["profile_sha256"] = "0" * 64
        self.assertTrue(
            any(
                "profile hash drifted" in finding
                for finding in validate_execution_manifest(
                    attacked,
                    authority=self.authority,
                )
            )
        )
        retry_attack = copy.deepcopy(self.manifest)
        retry_attack["attempt_policy"]["retryable_reason_codes"].append(
            "measurement_changed"
        )
        self.assertIn(
            "attempt policy differs from canonical authority",
            validate_execution_manifest(
                retry_attack,
                authority=self.authority,
            ),
        )
        runner_attack = copy.deepcopy(self.manifest)
        runner_attack["runner_release_sha256"] = "0" * 64
        self.assertIn(
            "runner release does not bind executable authority",
            validate_execution_manifest(
                runner_attack,
                authority=self.authority,
            ),
        )

    def test_canonical_typed_corpus_can_open_development_execution(self) -> None:
        canonical = canonical_authority()
        manifest = execution_manifest(canonical)
        self.assertEqual(
            validate_execution_manifest(manifest, authority=canonical),
            [],
        )

        attacked_authority = copy.deepcopy(canonical)
        attacked_episode = next(
            item
            for item in attacked_authority["catalog"]["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        del attacked_episode["acceptable_outcome"]["claim_targets"][0][
            "claim_target_kind"
        ]
        attacked_manifest = execution_manifest(attacked_authority)
        self.assertTrue(
            any(
                "lacks typed claim target kinds" in finding
                for finding in validate_execution_manifest(
                    attacked_manifest,
                    authority=attacked_authority,
                )
            )
        )

    def test_world_coverage_deduplicates_shared_outcome_authority(self) -> None:
        counts = _claim_target_kind_world_counts(
            {
                "cells": [
                    {
                        "business_world_id": "WORLD-WORDING-A",
                        "business_world_independence_key": (
                            "authority-set:" + "a" * 64
                        ),
                        "claim_target_kinds": ["contrast"],
                    },
                    {
                        "business_world_id": "WORLD-WORDING-B",
                        "business_world_independence_key": (
                            "authority-set:" + "a" * 64
                        ),
                        "claim_target_kinds": ["contrast"],
                    },
                    {
                        "business_world_id": "WORLD-INDEPENDENT",
                        "business_world_independence_key": (
                            "authority-set:" + "b" * 64
                        ),
                        "claim_target_kinds": ["contrast"],
                    },
                ]
            }
        )
        self.assertEqual({"contrast": 2}, counts)

    def test_full_mode_cannot_shrink_episode_or_operator_universe(self) -> None:
        attacked = copy.deepcopy(self.manifest)
        attacked["run_mode"] = "full"
        findings = validate_execution_manifest(
            attacked,
            authority=self.authority,
        )
        self.assertIn(
            "full run Episode set differs from canonical catalog",
            findings,
        )
        self.assertIn(
            "full run operator set differs from canonical registry",
            findings,
        )

    def test_execution_coordinates_cannot_be_duplicated(self) -> None:
        attacked = copy.deepcopy(self.manifest)
        duplicate = copy.deepcopy(attacked["cells"][0])
        duplicate["execution_cell_id"] = "CELL-DEV-002"
        attacked["cells"].append(duplicate)
        findings = validate_execution_manifest(
            attacked,
            authority=self.authority,
        )
        self.assertIn("execution coordinates must be unique", findings)

    def test_counterfactual_views_bind_materialized_episode(self) -> None:
        manifest = execution_manifest(
            self.authority,
            cells=[counterfactual_cell(self.authority)],
        )
        self.assertEqual(
            validate_execution_manifest(
                manifest,
                authority=self.authority,
            ),
            [],
        )
        manifest["cells"][0]["agent_world_view_sha256"] = "0" * 64
        self.assertTrue(
            any(
                "AgentWorldView hash drifted" in finding
                for finding in validate_execution_manifest(
                    manifest,
                    authority=self.authority,
                )
            )
        )

    def test_critical_floor_cannot_shrink_repeats(self) -> None:
        attacked = execution_manifest(
            self.authority,
            cells=[execution_cell(self.authority, critical=True)],
        )
        findings = validate_execution_manifest(
            attacked,
            authority=self.authority,
        )
        self.assertTrue(
            any("too few critical semantic paraphrases" in item for item in findings)
        )
        self.assertTrue(
            any("too few full-authority paraphrases" in item for item in findings)
        )

    def test_formal_manifest_rejects_unreviewed_profiles_and_held_out_gap(
        self,
    ) -> None:
        attacked = copy.deepcopy(self.manifest)
        attacked["execution_scope"] = "formal"
        attacked["status"] = "frozen"
        attacked["realm"] = "formal_conformance"
        findings = validate_execution_manifest(
            attacked,
            authority=self.authority,
        )
        self.assertTrue(
            any("source run manifest" in item for item in findings)
        )
        self.assertTrue(any("is not calibrated" in item for item in findings))
        self.assertIn("formal execution omits protected held-out cells", findings)

    def test_trace_requires_exact_acyclic_stage_graph(self) -> None:
        cell = self.manifest["cells"][0]
        bundle, artifact_index = trace_bundle(cell, self.manifest)
        self.assertEqual(
            validate_trace_bundle(
                bundle,
                cell,
                manifest=self.manifest,
                artifact_index=artifact_index,
            ),
            [],
        )
        bundle["stages"].pop()
        self.assertIn(
            "trace stage set is incomplete or unexpected",
            validate_trace_bundle(
                bundle,
                cell,
                manifest=self.manifest,
                artifact_index=artifact_index,
            ),
        )
        cyclic, cyclic_index = trace_bundle(cell, self.manifest)
        cyclic["stages"][0]["predecessor_stage_ids"] = [
            cyclic["stages"][-1]["stage_id"]
        ]
        self.assertIn(
            "trace predecessor graph contains a cycle",
            validate_trace_bundle(
                cyclic,
                cell,
                manifest=self.manifest,
                artifact_index=cyclic_index,
            ),
        )
        missing_edge, missing_edge_index = trace_bundle(cell, self.manifest)
        missing_edge["stages"][1]["predecessor_stage_ids"] = []
        self.assertIn(
            "trace predecessor graph differs from profile",
            validate_trace_bundle(
                missing_edge,
                cell,
                manifest=self.manifest,
                artifact_index=missing_edge_index,
                authority=self.authority,
            ),
        )
        forged, forged_index = trace_bundle(cell, self.manifest)
        forged["stages"][0]["artifact_sha256"] = "0" * 64
        self.assertTrue(
            any(
                "artifact_sha256 drifted" in finding
                for finding in validate_trace_bundle(
                    forged,
                    cell,
                    manifest=self.manifest,
                    artifact_index=forged_index,
                )
            )
        )

    def test_attempt_journal_selects_first_terminal_and_retains_failures(
        self,
    ) -> None:
        journal = attempt_journal(self.manifest)
        attempts = journal["cell_attempts"][0]["attempts"]
        attempts[0] = {
            **attempts[0],
            "disposition": "retryable_failure",
            "reason_code": "provider_transient_error",
        }
        attempts.append(
            {
                **attempts[0],
                "attempt_id": "ATTEMPT-CELL-DEV-001-2",
                "attempt_number": 2,
                "prior_attempt_id": "ATTEMPT-CELL-DEV-001-1",
                "disposition": "terminal_success",
                "reason_code": "completed",
            }
        )
        self.assertEqual(
            validate_attempt_journal(journal, manifest=self.manifest),
            [],
        )
        attempts.append(
            {
                **attempts[-1],
                "attempt_id": "ATTEMPT-CELL-DEV-001-3",
                "attempt_number": 3,
                "prior_attempt_id": "ATTEMPT-CELL-DEV-001-2",
            }
        )
        self.assertTrue(
            any(
                "continued after terminal attempt" in finding
                for finding in validate_attempt_journal(
                    journal,
                    manifest=self.manifest,
                )
            )
        )

    def test_reviewer_scores_map_mechanically_to_product_verdict(self) -> None:
        self.assertEqual(derive_review_verdict(evaluation_review()), "pass")
        self.assertEqual(
            derive_review_verdict(evaluation_review(disposition="needs_review")),
            "blocked",
        )
        self.assertEqual(
            derive_review_verdict(
                evaluation_review(critical=("authority_drift",))
            ),
            "fail",
        )
        low = evaluation_review()
        low["dimension_scores"]["investigation"] = 1
        self.assertEqual(derive_review_verdict(low), "fail")
        unsupported = evaluation_review()
        unsupported["claim_findings"][0]["status"] = "unsupported"
        unsupported["claim_findings"][0]["repair_target"] = "evidence"
        self.assertEqual(derive_review_verdict(unsupported), "fail")

    def test_cell_result_uses_strict_three_layer_and_reviewer_derivation(
        self,
    ) -> None:
        cell = self.manifest["cells"][0]
        bundle, artifact_index = trace_bundle(cell, self.manifest)
        journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(artifact_index),
        )
        invocations = model_invocations(self.authority, cell, bundle)
        hard_checks = hard_check_result(
            self.authority,
            self.manifest,
            cell,
            journal,
            bundle,
        )
        result = cell_result(
            self.manifest,
            journal,
            bundle,
            artifact_index,
            invocations,
            hard_checks,
        )
        self.assertEqual(
            validate_cell_result(
                result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                model_invocations=invocations,
                hard_check_result=hard_checks,
                authority=self.authority,
            ),
            [],
        )
        loop_invocations = copy.deepcopy(invocations)
        second_primary = copy.deepcopy(loop_invocations[0])
        second_primary["logical_model_job_id"] = "JOB-primary-followup"
        second_primary["provider_attempt_id"] = "PROVIDER-ATTEMPT-primary-followup"
        second_primary["provider_response_id"] = "RESPONSE-primary-followup"
        loop_invocations.append(second_primary)
        loop_result = copy.deepcopy(result)
        loop_result["model_invocation_set_sha256"] = canonical_sha256(
            loop_invocations
        )
        self.assertEqual(
            validate_cell_result(
                loop_result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                model_invocations=loop_invocations,
                hard_check_result=hard_checks,
                authority=self.authority,
            ),
            [],
        )
        forged_journal = copy.deepcopy(journal)
        forged_journal["cell_attempts"][0]["attempts"][0][
            "artifact_set_sha256"
        ] = "b" * 64
        forged_root_result = copy.deepcopy(result)
        forged_root_result["artifact_index_sha256"] = "b" * 64
        forged_root_result["attempt_journal_sha256"] = canonical_sha256(
            forged_journal
        )
        forged_root_checks = copy.deepcopy(hard_checks)
        forged_root_checks["artifact_index_sha256"] = "b" * 64
        forged_root_result["hard_check_result_sha256"] = canonical_sha256(
            forged_root_checks
        )
        self.assertTrue(
            any(
                "does not bind trace artifact index" in item
                for item in validate_cell_result(
                    forged_root_result,
                    manifest=self.manifest,
                    attempt_journal=forged_journal,
                    trace_bundle=bundle,
                    trace_artifact_index=artifact_index,
                    model_invocations=invocations,
                    hard_check_result=forged_root_checks,
                    authority=self.authority,
                )
            )
        )
        forged_invocations = copy.deepcopy(invocations)
        forged_invocations[0]["configuration_sha256"] = "0" * 64
        forged = copy.deepcopy(result)
        forged["model_invocation_set_sha256"] = canonical_sha256(
            forged_invocations
        )
        self.assertTrue(
            any(
                "configuration identity drifted" in item
                for item in validate_cell_result(
                    forged,
                    manifest=self.manifest,
                    attempt_journal=journal,
                    trace_bundle=bundle,
                    trace_artifact_index=artifact_index,
                    model_invocations=forged_invocations,
                    hard_check_result=hard_checks,
                    authority=self.authority,
                )
            )
        )
        result["evaluation_review"]["critical_failure_codes"] = [
            "authority_drift"
        ]
        self.assertTrue(
            any(
                "product behavior verdict must be fail" in item
                for item in validate_cell_result(
                    result,
                    manifest=self.manifest,
                    attempt_journal=journal,
                    trace_bundle=bundle,
                    trace_artifact_index=artifact_index,
                    model_invocations=invocations,
                    hard_check_result=hard_checks,
                    authority=self.authority,
                )
            )
        )
        result["layer_verdicts"]["product_behavior"] = "fail"
        result["critical_vetoes"] = ["authority_drift"]
        self.assertEqual(derive_cell_final_verdict(result), "fail")

        omitted_predicate = copy.deepcopy(loop_result)
        omitted_predicate["evaluation_review"][
            "evaluated_predicate_ids"
        ].pop()
        self.assertIn(
            "evaluation review predicate set differs from grader registry",
            validate_cell_result(
                omitted_predicate,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                model_invocations=loop_invocations,
                hard_check_result=hard_checks,
                authority=self.authority,
            ),
        )

    def test_hard_check_layers_are_derived_from_complete_registry_set(
        self,
    ) -> None:
        journal = attempt_journal(self.manifest)
        cell = self.manifest["cells"][0]
        bundle, _ = trace_bundle(cell, self.manifest)
        hard_checks = hard_check_result(
            self.authority,
            self.manifest,
            cell,
            journal,
            bundle,
        )
        self.assertEqual(
            validate_hard_check_result(
                hard_checks,
                manifest=self.manifest,
                cell=cell,
                terminal_attempt_id="ATTEMPT-CELL-DEV-001-1",
                trace_bundle=bundle,
                artifact_index_sha256=SHA,
                grader_registry=self.authority["grader_registry"],
            ),
            [],
        )
        hard_checks["checks"].pop()
        self.assertIn(
            "hard check set differs from grader registry",
            validate_hard_check_result(
                hard_checks,
                manifest=self.manifest,
                cell=cell,
                terminal_attempt_id="ATTEMPT-CELL-DEV-001-1",
                trace_bundle=bundle,
                artifact_index_sha256=SHA,
                grader_registry=self.authority["grader_registry"],
            ),
        )

    def test_suite_exact_set_and_formal_status_are_independent(self) -> None:
        cell = self.manifest["cells"][0]
        bundle, artifact_index = trace_bundle(cell, self.manifest)
        journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(artifact_index),
        )
        invocations = model_invocations(self.authority, cell, bundle)
        hard_checks = hard_check_result(
            self.authority,
            self.manifest,
            cell,
            journal,
            bundle,
        )
        result = cell_result(
            self.manifest,
            journal,
            bundle,
            artifact_index,
            invocations,
            hard_checks,
        )
        trace_bundles = {cell["execution_cell_id"]: bundle}
        trace_indexes = {cell["execution_cell_id"]: artifact_index}
        invocations_by_cell = {cell["execution_cell_id"]: invocations}
        hard_checks_by_cell = {cell["execution_cell_id"]: hard_checks}
        suite = derive_suite_result(
            self.manifest,
            [result],
            attempt_journal=journal,
            trace_bundles=trace_bundles,
            trace_artifact_indexes=trace_indexes,
            model_invocations_by_cell=invocations_by_cell,
            hard_check_results=hard_checks_by_cell,
            authority=self.authority,
        )
        self.assertEqual(suite["local_execution_status"], "pass")
        self.assertEqual(suite["coverage_admission_status"], "blocked")
        self.assertIn("run_mode_not_full", suite["coverage_blockers"])
        self.assertEqual(suite["formal_admission_status"], "blocked")
        self.assertIn("development_execution_scope", suite["formal_blockers"])

        missing = derive_suite_result(
            self.manifest,
            [],
            attempt_journal=journal,
            trace_bundles=trace_bundles,
            trace_artifact_indexes=trace_indexes,
            model_invocations_by_cell=invocations_by_cell,
            hard_check_results=hard_checks_by_cell,
            authority=self.authority,
        )
        self.assertEqual(missing["local_execution_status"], "blocked")
        self.assertEqual(missing["missing_cell_ids"], ["CELL-DEV-001"])

        duplicate = derive_suite_result(
            self.manifest,
            [result, result],
            attempt_journal=journal,
            trace_bundles=trace_bundles,
            trace_artifact_indexes=trace_indexes,
            model_invocations_by_cell=invocations_by_cell,
            hard_check_results=hard_checks_by_cell,
            authority=self.authority,
        )
        self.assertEqual(duplicate["local_execution_status"], "invalid")
        self.assertEqual(duplicate["duplicate_cell_ids"], ["CELL-DEV-001"])

        attacked_manifest = copy.deepcopy(self.manifest)
        attacked_manifest["policy_sha256"] = "0" * 64
        attacked_bundle, attacked_index = trace_bundle(
            attacked_manifest["cells"][0],
            attacked_manifest,
        )
        attacked_journal = attempt_journal(
            attacked_manifest,
            artifact_set_sha256=trace_artifact_set_sha256(attacked_index),
        )
        attacked_invocations = model_invocations(
            self.authority,
            attacked_manifest["cells"][0],
            attacked_bundle,
        )
        attacked_hard_checks = hard_check_result(
            self.authority,
            attacked_manifest,
            attacked_manifest["cells"][0],
            attacked_journal,
            attacked_bundle,
        )
        attacked_result = cell_result(
            attacked_manifest,
            attacked_journal,
            attacked_bundle,
            attacked_index,
            attacked_invocations,
            attacked_hard_checks,
        )
        attacked_suite = derive_suite_result(
            attacked_manifest,
            [attacked_result],
            attempt_journal=attacked_journal,
            trace_bundles={"CELL-DEV-001": attacked_bundle},
            trace_artifact_indexes={"CELL-DEV-001": attacked_index},
            model_invocations_by_cell={
                "CELL-DEV-001": attacked_invocations
            },
            hard_check_results={"CELL-DEV-001": attacked_hard_checks},
            authority=self.authority,
        )
        self.assertEqual(attacked_suite["local_execution_status"], "invalid")
        self.assertIn(
            "execution_manifest_invalid",
            attacked_suite["coverage_blockers"],
        )

    def test_relation_result_requires_exact_members_and_derived_verdict(
        self,
    ) -> None:
        operator = next(
            item
            for item in self.authority["mutation_operators"]["operators"]
            if item["operator_id"] == "meaning_preserving_paraphrase"
        )
        anchor = execution_cell(self.authority, cell_id="CELL-REL-001")
        subject = execution_cell(self.authority, cell_id="CELL-REL-002")
        subject["repeat_index"] = 2
        subject["wording_variant_id"] = "paraphrase-1"
        subject["wording_sha256"] = "9" * 64
        relation_base = {
            "relation_group_id": "REL-PARAPHRASE-001",
            "operator_ref": operator["operator_id"],
            "operator_sha256": canonical_sha256(operator),
            "expected_relation": operator["expected_relation"],
        }
        anchor["relation_binding"] = {**relation_base, "member_role": "anchor"}
        subject["relation_binding"] = {
            **relation_base,
            "member_role": "subject",
        }
        manifest = execution_manifest(self.authority, cells=[anchor, subject])
        member_results = [
            {"execution_cell_id": "CELL-REL-001", "verdict": "pass"},
            {"execution_cell_id": "CELL-REL-002", "verdict": "pass"},
        ]
        result = {
            "artifact_type": "gate3_relation_result",
            "artifact_version": "gate3.relation-result.v1",
            "execution_manifest_sha256": canonical_sha256(manifest),
            "relation_group_id": "REL-PARAPHRASE-001",
            "operator_ref": operator["operator_id"],
            "operator_sha256": canonical_sha256(operator),
            "expected_relation": operator["expected_relation"],
            "member_cell_ids": ["CELL-REL-001", "CELL-REL-002"],
            "member_results": [
                {
                    "execution_cell_id": item["execution_cell_id"],
                    "cell_result_sha256": canonical_sha256(item),
                }
                for item in member_results
            ],
            "artifact_refs": [
                "artifact://relation/measurement",
                "artifact://relation/claim-boundary",
            ],
            "check_results": [
                {
                    "check_id": "measurement_identity_preserved",
                    "verdict": "pass",
                    "artifact_refs": ["artifact://relation/measurement"],
                },
                {
                    "check_id": "claim_boundary_preserved",
                    "verdict": "pass",
                    "artifact_refs": ["artifact://relation/claim-boundary"],
                }
            ],
            "derived_verdict": "pass",
        }
        self.assertEqual(
            validate_relation_result(
                result,
                manifest=manifest,
                authority=self.authority,
                cell_results=member_results,
            ),
            [],
        )
        result["member_cell_ids"].pop()
        self.assertIn(
            "relation result member set is incomplete",
            validate_relation_result(
                result,
                manifest=manifest,
                authority=self.authority,
                cell_results=member_results,
            ),
        )


if __name__ == "__main__":
    unittest.main()
