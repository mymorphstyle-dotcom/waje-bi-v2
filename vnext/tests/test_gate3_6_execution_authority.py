from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime
import json
import unittest

from gate1_fixtures import make_frame, make_measurement_design, make_question

from tools.compile_gate3_eval_views import compile_views
from tools.compile_gate3_execution_universe import build_readiness
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
    runtime_model_record_set_sha256,
    trace_artifact_set_sha256,
    validate_attempt_journal,
    validate_cell_result,
    validate_execution_manifest,
    validate_hard_check_result,
    validate_relation_result,
    validate_trace_bundle,
    _claim_target_kind_world_counts,
    _cell_artifact_map_key_findings,
    _runtime_model_global_identity_findings,
)
from tools.gate3_runtime_projection import (
    RuntimeProjectionError,
    project_runtime_model_execution,
)
from waje_vnext.storage.codec import (
    decode_durable_model_result,
    decode_logical_model_job,
    decode_provider_attempt_receipt,
    decode_provider_attempt_request,
    decode_run_trace_manifest,
)
from waje_vnext.domain.actions import (
    ActionKind,
    AgentActionProposal,
    ReviseFramePayload,
)
from waje_vnext.domain.authority import CaseLifecycle, InvestigationCase
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.context import (
    ContextUserMessageItem,
    build_context_packet,
)
from waje_vnext.domain.controller import PrimaryAgentRequest
from waje_vnext.domain.runtime_amendment import (
    FrameCandidateRecord,
    FrameReviewDisposition,
    FrameReviewProposal,
    FrameReviewRequest,
    MessageBindingDisposition,
    MessageBindingRequest,
    MessageImpactBinding,
    MessageImpactKind,
    MessageImpactProposal,
    ModelConfigurationIdentity,
    ProposedSemanticAssertion,
    SemanticAssertion,
    SemanticAssertionKind,
    SemanticSourceSpan,
    TypedSemanticBinding,
)
from waje_vnext.providers import chat_completions
from waje_vnext.domain.typed_decode import decode_typed_dataclass


SHA = "a" * 64
TEST_DOUBLE_ADMISSION_FINDINGS = [
    "model stage producer registry differs from the runtime capability baseline",
    "test-double producers cannot enter execution admission",
]


def runtime_configuration(profile, *, seed):
    configuration = {
        "execution_role": profile["role"],
        "provider_ref": profile["provider"],
        "endpoint_ref": "https://api.deepseek.com/v1/chat/completions",
        "protocol_ref": "openai-compatible-chat-completions.v1",
        "adapter_release_ref": "waje-vnext://providers/chat-completions.v1",
        "adapter_release_sha256": (
            chat_completions._ADAPTER_RELEASE_SHA256
        ),
        "model_ref": profile["model"],
        "thinking": profile["thinking"],
        "stable_parameters": {
            "temperature": 0.0,
            "top_p": 1.0,
            "tool_choice_policy": "contract_selected",
            "parallel_tool_calls": False,
            "seed": seed,
        },
        "delivery_policy_ref": "waje-vnext://runtime/provider-delivery.v1",
        "max_attempts": 3,
        "timeout_seconds": None,
    }
    configuration["configuration_sha256"] = canonical_sha256(configuration)
    return configuration


def profile_bindings(authority, *, seed):
    profiles = {
        profile["role"]: profile
        for profile in authority["grader_registry"]["evaluator_profiles"]
    }
    return {
        role: {
            "profile_ref": profile["profile_id"],
            "profile_sha256": canonical_sha256(profile),
            "runtime_configuration_sha256": runtime_configuration(
                profile,
                seed=seed,
            )["configuration_sha256"],
        }
        for role, profile in profiles.items()
    }


def execution_cell(
    authority,
    *,
    cell_id="CELL-DEV-001",
    episode_id="G3-USER-001",
):
    trace_profile = next(
        profile
        for profile in authority["trace_profiles"]["profiles"]
        if profile["lane"] == "semantic_frame"
    )
    episode = next(
        item
        for item in authority["catalog"]["episodes"]
        if item["episode_id"] == episode_id
    )
    corpus_entry = next(
        item
        for item in authority["corpus_registry"]["entries"]
        if item["episode_id"] == episode_id
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
        "source_run_cell_ref": f"candidate:{episode_id}:base",
        "episode_id": episode_id,
        "episode_core_sha256": core_sha,
        "case_variant": {"kind": "base"},
        "lane": "semantic_frame",
        "wording_variant_id": "base",
        "wording_authority_ref": f"episode:{episode_id}:base:base",
        "wording_sha256": canonical_sha256(
            [
                message["text"]
                for message in episode["user_episode"]["messages"]
                if message["turn"] <= 1
            ]
        ),
        "visible_turn": 1,
        "paraphrase_index": 0,
        "repeat_index": 1,
        "seed": 731,
        "risk_level": episode["decision_stakes"]["risk_level"],
        "source_pool": episode["source_pool"],
        "business_world_id": episode["business_world"]["world_id"],
        "business_world_independence_key": episode[
            "business_world_independence_key"
        ],
        "claim_target_kinds": sorted(
            {
                target["claim_target_kind"]
                for target in episode["acceptable_outcome"]["claim_targets"]
            }
        ),
        "coverage_atom_refs": episode_coverage_atom_refs(episode),
        "historical_regression": episode["source_pool"] == "historical_failure",
        "agent_world_view_sha256": views["agent_world_view"]["view_sha256"],
        "evaluator_oracle_view_sha256": views[
            "evaluator_oracle_view"
        ]["view_sha256"],
        "trace_profile_ref": trace_profile["profile_id"],
        "trace_profile_sha256": canonical_sha256(trace_profile),
        "role_profiles": profile_bindings(authority, seed=731),
        "required_stage_ids": trace_profile["required_stage_ids"],
    }


def counterfactual_cell(
    authority,
    *,
    cell_id="CELL-CF-001",
    sibling_index=0,
):
    cell = execution_cell(authority, cell_id=cell_id)
    episode = next(
        item
        for item in authority["catalog"]["episodes"]
        if item["episode_id"] == "G3-USER-001"
    )
    sibling = episode["counterfactual_siblings"][sibling_index]
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
            "wording_authority_ref": (
                f"episode:G3-USER-001:{sibling['sibling_id']}:base"
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


def execution_manifest(authority, *, cells=None, relation_groups=None):
    resolved_cells = cells or [execution_cell(authority)]
    standalone = next(
        item
        for item in authority["mutation_operators"]["operators"]
        if item["operator_id"] == "episode_outcome"
    )
    resolved_relation_groups = relation_groups or [
        {
            "relation_group_id": f"REL-{cell['execution_cell_id']}",
            "operator_ref": standalone["operator_id"],
            "operator_sha256": canonical_sha256(standalone),
            "expected_relation": standalone["expected_relation"],
            "scenario_binding": None,
            "members": [
                {
                    "execution_cell_id": cell["execution_cell_id"],
                    "member_role": "singleton",
                }
            ],
        }
        for cell in resolved_cells
    ]
    readiness = build_readiness(
        policy=authority["policy"],
        catalog=authority["catalog"],
        paraphrase_registry=authority["paraphrase_authority"],
        operator_registry=authority["mutation_operators"],
        scenario_registry=authority["operator_scenario_authority"],
        trace_profiles=authority["trace_profiles"],
        grader_registry=authority["grader_registry"],
        source_run_manifest=authority["source_run_manifest"],
        held_out_manifest=authority["protected_held_out_manifest"],
    )
    return {
        "artifact_type": "gate3_execution_manifest",
        "artifact_version": "gate3.execution-manifest.v2",
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
        "paraphrase_authority_registry_sha256": canonical_sha256(
            authority["paraphrase_authority"]
        ),
        "operator_scenario_authority_registry_sha256": canonical_sha256(
            authority["operator_scenario_authority"]
        ),
        "protected_held_out_manifest_sha256": canonical_sha256(
            authority["protected_held_out_manifest"]
        ),
        "execution_universe_compiler_sha256": readiness["authority_hashes"][
            "compiler_release_sha256"
        ],
        "required_coordinate_set_sha256": readiness["universe_summary"][
            "required_coordinate_set_sha256"
        ],
        "required_episode_relation_group_set_sha256": readiness[
            "universe_summary"
        ]["required_episode_relation_group_set_sha256"],
        "required_operator_scenario_coordinate_set_sha256": readiness[
            "universe_summary"
        ]["required_operator_scenario_coordinate_set_sha256"],
        "required_operator_scenario_relation_group_set_sha256": readiness[
            "universe_summary"
        ]["required_operator_scenario_relation_group_set_sha256"],
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
        "cells": resolved_cells,
        "relation_groups": resolved_relation_groups,
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


def model_output_payload(stage_id):
    if stage_id == "evaluation_review":
        return evaluation_review()
    if stage_id == "typed_binding":
        return to_jsonable(
            MessageImpactProposal(
                impact_kind=MessageImpactKind.QUESTION_REVISION,
                disposition=MessageBindingDisposition.ACCEPTED,
                assertions=(
                    ProposedSemanticAssertion(
                        kind=SemanticAssertionKind.BUSINESS_CONSTRAINT,
                        value_json='{"business_request":"分析业务问题"}',
                        source_start_codepoint=0,
                        source_end_codepoint=6,
                        material=True,
                    ),
                ),
                ambiguities=(),
                clarification_options=(),
                recommended_option_id=None,
            )
        )
    if stage_id == "frame_proposal":
        question_id = "CASE-G36-001:question:1"
        return to_jsonable(
            AgentActionProposal(
                kind=ActionKind.REVISE_FRAME,
                payload=ReviseFramePayload(
                    question_revision_id=question_id,
                    revision_reason_ref="reason:define-current-measurement",
                    measurement_design=make_measurement_design(
                        question_id=question_id,
                        include_source_span=False,
                    ),
                ),
            )
        )
    if stage_id == "frame_review":
        return to_jsonable(
            FrameReviewProposal(
                disposition=FrameReviewDisposition.ACCEPT,
                objections=(),
                review_summary="Measurement design is coherent.",
            )
        )
    if stage_id in {"claim_proposal", "runtime_review"}:
        return {"stage_id": stage_id, "status": "unprovisioned"}
    raise AssertionError(f"unregistered model stage {stage_id}")


def runtime_typed_request(stage_id, *, job_id, cell, bundle):
    requested_at = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    case_id = bundle["case_id"]
    question = make_question(case_id=case_id)
    message_id = question.source_messages[0].message_id
    message_content = question.source_messages[0].content
    binding_id = "BINDING-G36-001"
    if stage_id == "typed_binding":
        return to_jsonable(
            MessageBindingRequest(
                logical_model_job_id=job_id,
                case_id=case_id,
                message_id=message_id,
                message_content=message_content,
                prior_question_text=None,
                has_accepted_frame=False,
                binding_contract_ref=(
                    "waje-vnext://contracts/domain/"
                    "message-impact-binding.v1"
                ),
                requested_at=requested_at,
            )
        )
    question = replace(
        question,
        explicit_constraint_refs=(binding_id,),
    )
    if stage_id in {"frame_proposal", "claim_proposal"}:
        case = InvestigationCase(
            case_id=case_id,
            thread_id="THREAD-G36-001",
            lifecycle=CaseLifecycle.OPEN,
            head_version=1,
            accepted_question_revision_id=question.question_revision_id,
            accepted_frame_revision_id=None,
            accepted_plan_revision_id=None,
            accepted_answer_version_id=None,
            analysis_cycle_id=question.analysis_cycle_id,
            opened_at=requested_at,
            updated_at=requested_at,
        )
        context_packet = build_context_packet(
            packet_id=f"PACKET-{cell['execution_cell_id']}",
            case=case,
            user_messages=(
                ContextUserMessageItem(
                    message_id=message_id,
                    sequence=1,
                    authority_epoch=1,
                    kind="user_message",
                    content=message_content,
                ),
            ),
            relevant_event_cursor_start=0,
            relevant_event_cursor_end=0,
            accepted_question=question,
            accepted_frame=None,
            accepted_plan=None,
            accepted_answer=None,
            recent_events=(),
            evidence_index=(),
            decision_index=(),
            reviewer_objection_index=(),
            built_at=requested_at,
        )
        action_kind = (
            ActionKind.PROPOSE_ANSWER
            if stage_id == "claim_proposal"
            else ActionKind.REVISE_FRAME
        )
        return to_jsonable(
            PrimaryAgentRequest(
                turn_id=f"TURN-{stage_id}",
                run_id=bundle["run_id"],
                context_packet=context_packet,
                allowed_actions=(action_kind,),
                action_contract_ref=(
                    "waje-vnext://contracts/domain/actions.v3"
                ),
                requested_at=requested_at,
            )
        )
    if stage_id == "frame_review":
        span = SemanticSourceSpan(
            span_id="SPAN-G36-001",
            message_id=message_id,
            start_codepoint=0,
            end_codepoint=len(message_content),
            selected_text_sha256=content_sha256(message_content),
        )
        assertion = SemanticAssertion(
            assertion_id="ASSERTION-G36-001",
            kind=SemanticAssertionKind.BUSINESS_CONSTRAINT,
            value={"business_request": message_content},
            source_span_ids=(span.span_id,),
            decision_record_ids=(),
            material=True,
        )
        semantic_binding = TypedSemanticBinding(
            binding_contract_version="message-impact-binding.v1",
            source_spans=(span,),
            assertions=(assertion,),
            ambiguities=(),
            decision_ledger_refs=(),
        )
        binding = MessageImpactBinding(
            binding_id=binding_id,
            pending_message_id="PENDING-G36-001",
            case_id=case_id,
            message_id=message_id,
            authority_epoch=1,
            source_payload_sha256=content_sha256(
                {"message": message_content}
            ),
            impact_kind=MessageImpactKind.QUESTION_REVISION,
            disposition=MessageBindingDisposition.ACCEPTED,
            bound_question_revision_id=question.question_revision_id,
            prior_frame_revision_id=None,
            decision_record_ids=(),
            semantic_binding=semantic_binding,
            semantic_binding_sha256=semantic_binding.content_sha256,
            logical_model_job_id="JOB-typed_binding-accepted",
            created_at=requested_at,
        )
        frame = make_frame(
            frame_id=f"FRAME-REVISION-{cell['execution_cell_id']}",
            action_id="ACTION-FRAME-G36-001",
            question=question,
            measurement_design=make_measurement_design(
                question_id=question.question_revision_id,
                include_source_span=False,
            ),
            case_id=case_id,
        )
        candidate = FrameCandidateRecord(
            frame_candidate_id=f"FRAME-{cell['execution_cell_id']}",
            case_id=case_id,
            message_binding_id=binding.binding_id,
            question_revision_id=question.question_revision_id,
            proposed_frame_revision_id=frame.frame_revision_id,
            proposed_frame_content_sha256=frame.content_sha256,
            proposed_frame=frame,
            candidate_generation=1,
            prior_frame_candidate_id=None,
            addressed_objection_ids=(),
            authority_epoch=1,
            source_action_id="ACTION-FRAME-G36-001",
            source_operation_id="OPERATION-FRAME-G36-001",
            review_job_id=job_id,
            created_at=requested_at,
        )
        return to_jsonable(
            FrameReviewRequest(
                logical_model_job_id=job_id,
                case_id=case_id,
                frame_candidate=candidate,
                accepted_question=question,
                accepted_message_bindings=(binding,),
                prior_frame_review=None,
                objection_closures=(),
                deterministic_validation_findings=(),
                review_contract_ref=(
                    "waje-vnext://contracts/domain/measurement-review.v1"
                ),
                reviewer_configuration_ref=(
                    "RUNTIME-REVIEWER-DEEPSEEK-PRO-NOTHINK-V1"
                ),
                independence_policy_ref=(
                    "waje-vnext://runtime/reviewer-role-separation.v1"
                ),
                requested_at=requested_at,
            )
        )
    if stage_id == "evaluation_review":
        return {"evaluation_input_id": f"EVAL-{cell['execution_cell_id']}"}
    raise AssertionError(f"unregistered typed request stage {stage_id}")


def runtime_model_execution(
    authority,
    cell,
    manifest,
    bundle,
    artifact_index,
    *,
    stage_id,
    suffix="accepted",
    artifact_ref=None,
):
    contracts = {
        item["stage_id"]: item
        for item in authority["trace_profiles"][
            "model_stage_producer_contracts"
        ]
    }
    contract = contracts[stage_id]
    profiles = {
        profile["role"]: profile
        for profile in authority["grader_registry"]["evaluator_profiles"]
    }
    profile_role = (
        "primary_business_analysis_agent"
        if contract["evaluation_role"] == "message_binding"
        else contract["evaluation_role"]
    )
    profile = profiles[profile_role]
    job_id = f"JOB-{stage_id}-{suffix}"
    result_id = f"DURABLE-RESULT-{stage_id}-{suffix}"
    attempt_id = f"PROVIDER-ATTEMPT-{stage_id}-{suffix}-1"
    receipt_id = f"PROVIDER-RECEIPT-{stage_id}-{suffix}-1"
    if artifact_ref is None:
        artifact_ref = f"artifact://{stage_id}"
    configuration = runtime_configuration(profile, seed=cell["seed"])
    typed_request = runtime_typed_request(
        stage_id,
        job_id=job_id,
        cell=cell,
        bundle=bundle,
    )
    if contract["producer_status"] == "runtime_implemented":
        request_type = {
            "primary_agent": PrimaryAgentRequest,
            "message_binding": MessageBindingRequest,
            "measurement_reviewer": FrameReviewRequest,
        }[contract["logical_job_kind"]]
        request_record = decode_typed_dataclass(
            request_type,
            typed_request,
        )
        configuration_record = decode_typed_dataclass(
            ModelConfigurationIdentity,
            configuration,
        )
        compiled = chat_completions.compile_trusted_chat_invocation(
            logical_job_kind=contract["logical_job_kind"],
            request=request_record,
            configuration=configuration_record,
        )
        body = to_jsonable(compiled.payload)
        input_view_ref = compiled.input_view_ref
        input_view_sha256 = compiled.input_view_sha256
        system_instruction = compiled.system_instruction
        tools = to_jsonable(compiled.tools)
    else:
        input_view_ref = typed_request["evaluation_input_id"]
        input_view_sha256 = canonical_sha256(typed_request)
        system_instruction = f"contract:{stage_id}"
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "submit_evaluation_review",
                    "parameters": {"type": "object"},
                },
            }
        ]
        body = {
            "model": configuration["model_ref"],
            "thinking": {"type": configuration["thinking"]},
            "temperature": configuration["stable_parameters"][
                "temperature"
            ],
            "top_p": configuration["stable_parameters"]["top_p"],
            "messages": [
                {"role": "system", "content": system_instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        typed_request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "tools": tools,
            "tool_choice": {
                "type": "function",
                "function": {"name": "submit_evaluation_review"},
            },
            "parallel_tool_calls": False,
            "seed": cell["seed"],
        }
    messages = body["messages"]
    tool_sha256 = canonical_sha256(tools)
    artifact = {
        "model_request_artifact_id": f"MODEL-REQUEST-{job_id}",
        "logical_model_job_id": job_id,
        "execution_role": contract["execution_role"],
        "logical_job_kind": contract["logical_job_kind"],
        "input_view_kind": contract["input_view_kind"],
        "input_view_ref": input_view_ref,
        "input_view_sha256": input_view_sha256,
        "typed_request_contract_ref": contract[
            "typed_request_contract_ref"
        ],
        "typed_request_sha256": canonical_sha256(typed_request),
        "prompt_bundle_ref": contract["prompt_bundle_ref"],
        "prompt_bundle_sha256": canonical_sha256({"messages": [messages[0]]}),
        "tool_bundle_ref": contract["tool_bundle_ref"],
        "tool_bundle_sha256": tool_sha256,
        "output_contract_ref": contract["output_contract_ref"],
        "output_contract_sha256": canonical_sha256(
            {
                "output_contract_ref": contract["output_contract_ref"],
                "tool_bundle_sha256": tool_sha256,
                "decoder_release_ref": contract["decoder_release_ref"],
                "decoder_release_sha256": configuration[
                    "adapter_release_sha256"
                ],
            }
        ),
        "decoder_release_ref": contract["decoder_release_ref"],
        "decoder_release_sha256": configuration["adapter_release_sha256"],
        "provider_request_body": body,
        "provider_request_sha256": canonical_sha256(body),
        "created_at": "2026-07-31T12:00:00+00:00",
    }
    artifact_sha256 = canonical_sha256(artifact)
    stage = next(
        item for item in bundle["stages"] if item["stage_id"] == stage_id
    )
    job = {
        "logical_model_job_id": job_id,
        "case_id": bundle["case_id"],
        "job_id": job_id,
        "operation_id": f"OP-{stage_id}-{suffix}",
        "role": contract["logical_job_kind"],
        "provider_ref": configuration["provider_ref"],
        "model_ref": configuration["model_ref"],
        "prompt_contract_ref": artifact["prompt_bundle_ref"],
        "input_sha256": artifact["typed_request_sha256"],
        "configuration_identity": configuration,
        "configuration_sha256": configuration["configuration_sha256"],
        "model_request_artifact": artifact,
        "model_request_artifact_sha256": artifact_sha256,
        "authority_snapshot_sha256": stage["authority_snapshot_sha256"],
        "created_at": "2026-07-31T12:00:00+00:00",
    }
    request = {
        "provider_attempt_id": attempt_id,
        "logical_model_job_id": job_id,
        "attempt_number": 1,
        "prior_provider_attempt_id": None,
        "provider_idempotency_key": f"IDEMPOTENCY-{stage_id}-{suffix}-1",
        "request_sha256": artifact["provider_request_sha256"],
        "model_request_artifact_sha256": artifact_sha256,
        "configuration_sha256": configuration["configuration_sha256"],
        "requested_at": "2026-07-31T12:00:00+00:00",
    }
    output_payload = model_output_payload(stage_id)
    output_sha256 = canonical_sha256(output_payload)
    receipt = {
        "provider_attempt_receipt_id": receipt_id,
        "provider_attempt_id": attempt_id,
        "logical_model_job_id": job_id,
        "disposition": "succeeded",
        "provider_response_id": f"RESPONSE-{stage_id}-{suffix}",
        "output_sha256": output_sha256,
        "finish_reason": "tool_calls",
        "usage_payload": {},
        "completed_at": "2026-07-31T12:00:01+00:00",
    }
    result = {
        "durable_model_result_id": result_id,
        "logical_model_job_id": job_id,
        "provider_attempt_id": attempt_id,
        "provider_attempt_receipt_id": receipt_id,
        "result_kind": contract["logical_job_kind"],
        "result_contract_ref": contract["output_contract_ref"],
        "result_payload": output_payload,
        "output_sha256": output_sha256,
        "model_request_artifact_sha256": artifact_sha256,
        "configuration_sha256": configuration["configuration_sha256"],
        "recorded_at": "2026-07-31T12:00:01+00:00",
    }
    execution = {
        "artifact_type": "gate3_runtime_model_execution",
        "artifact_version": "gate3.runtime-model-execution.v1",
        "execution_manifest_sha256": canonical_sha256(manifest),
        "execution_cell_id": cell["execution_cell_id"],
        "execution_attempt_id": f"ATTEMPT-{cell['execution_cell_id']}-1",
        "run_trace_manifest_id": bundle["persisted_run_trace_manifest"][
            "trace_manifest_id"
        ],
        "run_trace_manifest_sha256": canonical_sha256(
            bundle["persisted_run_trace_manifest"]
        ),
        "evaluator_profile_ref": profile["profile_id"],
        "evaluator_profile_sha256": canonical_sha256(profile),
        "source_proof": {
            "mode": "development_self_attested",
            "runtime_store_ref": "memory://gate3-test-runtime",
            "snapshot_ref": f"snapshot://{cell['execution_cell_id']}",
            "export_attestation_ref": None,
        },
        "logical_model_job": job,
        "attempts": [{"request": request, "receipt": receipt}],
        "durable_result": result,
        "trace_output_binding": {
            "stage_id": stage_id,
            "artifact_ref": artifact_ref,
        },
    }
    execution["runtime_record_set_sha256"] = (
        runtime_model_record_set_sha256(execution)
    )
    record = next(
        (
            item
            for item in artifact_index["records"]
            if item["artifact_ref"] == artifact_ref
        ),
        None,
    )
    if record is None:
        artifact_index["records"].append(
            {
                "artifact_ref": artifact_ref,
                "artifact_sha256": output_sha256,
                "artifact_kind": "typed_model_result",
                "authority_source_kind": "durable_model_result",
                "authority_source_ref": result_id,
                "run_id": bundle["run_id"],
                "case_id": bundle["case_id"],
                "correlation_id": bundle["correlation_id"],
                "journal_cursor": stage["journal_cursor"],
                "authority_snapshot_sha256": stage[
                    "authority_snapshot_sha256"
                ],
            }
        )
    else:
        record.update(
            {
                "artifact_sha256": output_sha256,
                "artifact_kind": "typed_model_result",
                "authority_source_kind": "durable_model_result",
                "authority_source_ref": result_id,
            }
        )
    persisted_manifest = bundle["persisted_run_trace_manifest"]
    for field, value in (
        ("logical_model_job_ids", job_id),
        ("provider_attempt_request_ids", attempt_id),
        ("provider_attempt_receipt_ids", receipt_id),
        ("durable_model_result_ids", result_id),
    ):
        if value not in persisted_manifest[field]:
            persisted_manifest[field].append(value)
    refresh_persisted_run_trace_manifest(bundle, artifact_index)
    execution["run_trace_manifest_sha256"] = canonical_sha256(
        bundle["persisted_run_trace_manifest"]
    )
    return execution


def runtime_model_executions(authority, cell, manifest, bundle, artifact_index):
    model_stage_ids = [
        stage_id
        for stage_id in cell["required_stage_ids"]
        if stage_id
        in {
            contract["stage_id"]
            for contract in authority["trace_profiles"][
                "model_stage_producer_contracts"
            ]
        }
    ]
    executions = [
        runtime_model_execution(
            authority,
            cell,
            manifest,
            bundle,
            artifact_index,
            stage_id=stage_id,
        )
        for stage_id in model_stage_ids
    ]
    bind_executions_to_run_trace(executions, bundle)
    return executions


def bind_executions_to_run_trace(executions, bundle):
    manifest_sha256 = canonical_sha256(
        bundle["persisted_run_trace_manifest"]
    )
    for execution in executions:
        execution["run_trace_manifest_sha256"] = manifest_sha256


def rehash_runtime_model_execution(execution):
    job = execution["logical_model_job"]
    configuration = job["configuration_identity"]
    configuration["configuration_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in configuration.items()
            if key != "configuration_sha256"
        }
    )
    artifact = job["model_request_artifact"]
    body = artifact["provider_request_body"]
    artifact["provider_request_sha256"] = canonical_sha256(body)
    artifact["prompt_bundle_sha256"] = canonical_sha256(
        {"messages": [body["messages"][0]]}
    )
    artifact["tool_bundle_sha256"] = canonical_sha256(body["tools"])
    artifact["typed_request_sha256"] = canonical_sha256(
        json.loads(body["messages"][1]["content"])
    )
    artifact["output_contract_sha256"] = canonical_sha256(
        {
            "output_contract_ref": artifact["output_contract_ref"],
            "tool_bundle_sha256": artifact["tool_bundle_sha256"],
            "decoder_release_ref": artifact["decoder_release_ref"],
            "decoder_release_sha256": artifact["decoder_release_sha256"],
        }
    )
    artifact_sha256 = canonical_sha256(artifact)
    job["provider_ref"] = configuration["provider_ref"]
    job["model_ref"] = configuration["model_ref"]
    job["prompt_contract_ref"] = artifact["prompt_bundle_ref"]
    job["input_sha256"] = artifact["typed_request_sha256"]
    job["configuration_sha256"] = configuration["configuration_sha256"]
    job["model_request_artifact_sha256"] = artifact_sha256
    for attempt in execution["attempts"]:
        request = attempt["request"]
        request["logical_model_job_id"] = job["logical_model_job_id"]
        request["request_sha256"] = artifact["provider_request_sha256"]
        request["model_request_artifact_sha256"] = artifact_sha256
        request["configuration_sha256"] = configuration[
            "configuration_sha256"
        ]
        attempt["receipt"]["logical_model_job_id"] = job[
            "logical_model_job_id"
        ]
    result = execution["durable_result"]
    result["logical_model_job_id"] = job["logical_model_job_id"]
    result["result_kind"] = job["role"]
    result["result_contract_ref"] = artifact["output_contract_ref"]
    result["output_sha256"] = canonical_sha256(result["result_payload"])
    result["model_request_artifact_sha256"] = artifact_sha256
    result["configuration_sha256"] = configuration["configuration_sha256"]
    final = execution["attempts"][-1]
    result["provider_attempt_id"] = final["request"]["provider_attempt_id"]
    result["provider_attempt_receipt_id"] = final["receipt"][
        "provider_attempt_receipt_id"
    ]
    final["receipt"]["provider_attempt_id"] = final["request"][
        "provider_attempt_id"
    ]
    final["receipt"]["output_sha256"] = result["output_sha256"]
    execution["runtime_record_set_sha256"] = (
        runtime_model_record_set_sha256(execution)
    )


RUN_TRACE_ID_FIELDS = (
    "ingress_record_ids",
    "message_binding_ids",
    "frame_candidate_ids",
    "frame_candidate_supersession_ids",
    "frame_review_ids",
    "job_disposition_record_ids",
    "logical_model_job_ids",
    "provider_attempt_request_ids",
    "provider_attempt_receipt_ids",
    "durable_model_result_ids",
    "plan_revision_ids",
    "resolution_outcome_ids",
    "obligation_ids",
    "effect_attempt_ids",
    "evidence_record_ids",
    "claim_ids",
    "provisional_answer_version_ids",
)


def refresh_persisted_run_trace_manifest(bundle, artifact_index):
    persisted = bundle["persisted_run_trace_manifest"]
    lineage = {
        "case_id": persisted["case_id"],
        "run_id": persisted["run_id"],
        "trace_profile": persisted["trace_profile"],
        "start_event_cursor": persisted["start_event_cursor"],
        "terminal_event_cursor": persisted["terminal_event_cursor"],
        "event_operation_lineage": persisted["event_operation_lineage"],
        **{field: persisted[field] for field in RUN_TRACE_ID_FIELDS},
    }
    persisted["lineage_sha256"] = canonical_sha256(lineage)
    persisted_sha256 = canonical_sha256(persisted)
    bundle["persisted_run_trace_manifest_sha256"] = persisted_sha256
    record = next(
        (
            item
            for item in artifact_index["records"]
            if item["artifact_ref"]
            == bundle["persisted_run_trace_manifest_ref"]
        ),
        None,
    )
    if record is not None:
        record["artifact_sha256"] = persisted_sha256
        record["authority_source_ref"] = persisted["trace_manifest_id"]


def complete_cell_artifacts(authority, manifest):
    cell = manifest["cells"][0]
    bundle, artifact_index = trace_bundle(cell, manifest)
    executions = runtime_model_executions(
        authority,
        cell,
        manifest,
        bundle,
        artifact_index,
    )
    journal = attempt_journal(
        manifest,
        artifact_set_sha256=trace_artifact_set_sha256(artifact_index),
    )
    hard_checks = hard_check_result(
        authority,
        manifest,
        cell,
        journal,
        bundle,
    )
    result = cell_result(
        manifest,
        journal,
        bundle,
        artifact_index,
        executions,
        hard_checks,
    )
    return (
        cell,
        bundle,
        artifact_index,
        executions,
        journal,
        hard_checks,
        result,
    )


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
    runtime_executions,
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
        "runtime_model_execution_set_sha256": canonical_sha256(
            runtime_executions
        ),
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
    model_stage_ids = {
        "typed_binding",
        "frame_proposal",
        "frame_review",
        "claim_proposal",
        "runtime_review",
        "evaluation_review",
    }
    stage_event_types = {
        "message_ingress": "message_ingressed",
        "typed_binding": "message_binding_completed",
        "frame_proposal": "llm_job_completed",
        "frame_review": "reviewer_job_completed",
        "frame_disposition": "frame_accepted",
        "plan_acceptance": "plan_accepted",
        "effect_dispatch": "effect_enqueued",
        "effect_receipt": "effect_completed",
        "evidence_disposition": "evidence_recorded",
        "claim_proposal": "llm_job_completed",
        "runtime_review": "reviewer_job_completed",
        "settlement_boundary": "settlement_precondition_recorded",
        "workflow_projection": "workflow_projection_applied",
        "evaluation_review": "reviewer_job_completed",
    }
    stage_authority_refs = {
        "message_ingress": "CASE-G36-001:message:1",
        "frame_disposition": f"FRAME-REVISION-{cell['execution_cell_id']}",
        "plan_acceptance": "PLAN-G36-001",
        "effect_dispatch": "EFFECT-ATTEMPT-G36-001",
        "effect_receipt": "EFFECT-ATTEMPT-G36-001",
        "evidence_disposition": "EVIDENCE-G36-001",
        "settlement_boundary": "SETTLEMENT-G36-001",
        "workflow_projection": "WORKFLOW-G36-001",
    }
    for index, stage_id in enumerate(cell["required_stage_ids"], start=1):
        stages.append(
            {
                "stage_id": stage_id,
                "artifact_ref": f"artifact://{stage_id}",
                "artifact_sha256": (
                    canonical_sha256(model_output_payload(stage_id))
                    if stage_id in model_stage_ids
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
        "persisted_run_trace_manifest_sha256": "0" * 64,
        "persisted_run_trace_manifest": {
            "trace_manifest_id": "RUN-TRACE-MANIFEST-G36-001",
            "case_id": "CASE-G36-001",
            "run_id": "RUN-G36-001",
            "trace_profile": "case_authority_lane",
            "start_event_cursor": 1,
            "terminal_event_cursor": len(stages),
            "event_operation_lineage": [
                {
                    "cursor": stage["journal_cursor"],
                    "event_id": f"EVENT-{stage['stage_id']}",
                    "event_type": stage_event_types[stage["stage_id"]],
                    "recorded_at": "2026-07-31T12:00:01+00:00",
                    "operation_id": f"OPERATION-{stage['stage_id']}",
                    "causation_id": (
                        "USER-INGRESS"
                        if index == 0
                        else f"OPERATION-{stages[index - 1]['stage_id']}"
                    ),
                    "correlation_id": "RUN-G36-001",
                    "authority_revision": index + 1,
                    "action_id": (
                        "ACTION-FRAME-G36-001"
                        if stage["stage_id"] == "frame_disposition"
                        else None
                    ),
                    "authority_ref": stage_authority_refs.get(
                        stage["stage_id"]
                    ),
                    "payload_sha256": stage["artifact_sha256"],
                    "event_content_sha256": stage["artifact_sha256"],
                }
                for index, stage in enumerate(stages)
            ],
            "ingress_record_ids": ["INGRESS-G36-001"],
            "message_binding_ids": ["BINDING-G36-001"],
            "frame_candidate_ids": [f"FRAME-{cell['execution_cell_id']}"],
            "frame_candidate_supersession_ids": [],
            "frame_review_ids": [f"FRAME-REVIEW-{cell['execution_cell_id']}"],
            "job_disposition_record_ids": [],
            "logical_model_job_ids": [],
            "provider_attempt_request_ids": [],
            "provider_attempt_receipt_ids": [],
            "durable_model_result_ids": [],
            "plan_revision_ids": (
                ["PLAN-G36-001"] if cell["lane"] == "full_authority" else []
            ),
            "resolution_outcome_ids": (
                ["RESOLUTION-G36-001"]
                if cell["lane"] == "full_authority"
                else []
            ),
            "obligation_ids": (
                ["OBLIGATION-G36-001"]
                if cell["lane"] == "full_authority"
                else []
            ),
            "effect_attempt_ids": (
                ["EFFECT-ATTEMPT-G36-001"]
                if cell["lane"] == "full_authority"
                else []
            ),
            "evidence_record_ids": (
                ["EVIDENCE-G36-001"]
                if cell["lane"] == "full_authority"
                else []
            ),
            "claim_ids": (
                ["CLAIM-G36-001"] if cell["lane"] == "full_authority" else []
            ),
            "provisional_answer_version_ids": (
                ["ANSWER-G36-001"] if cell["lane"] == "full_authority" else []
            ),
            "lineage_sha256": "0" * 64,
            "built_at": "2026-07-31T12:00:02+00:00",
        },
        "stages": stages,
    }
    records = [
        {
            "artifact_ref": stage["artifact_ref"],
            "artifact_sha256": stage["artifact_sha256"],
            "artifact_kind": (
                "typed_model_result"
                if stage["stage_id"] in model_stage_ids
                else "authority_record"
            ),
            "authority_source_kind": (
                "durable_model_result"
                if stage["stage_id"] in model_stage_ids
                else "event_journal"
            ),
            "authority_source_ref": (
                f"DURABLE-RESULT-{stage['stage_id']}-accepted"
                if stage["stage_id"] in model_stage_ids
                else f"EVENT-{stage['stage_id']}"
            ),
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
            "artifact_sha256": "0" * 64,
            "artifact_kind": "run_trace_manifest",
            "authority_source_kind": "run_trace_manifest",
            "authority_source_ref": "RUN-TRACE-MANIFEST-G36-001",
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
        "artifact_version": "gate3.trace-artifact-index.v2",
        "execution_manifest_sha256": canonical_sha256(manifest),
        "records": records,
    }
    refresh_persisted_run_trace_manifest(bundle, artifact_index)
    return bundle, artifact_index


class Gate36ExecutionAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = copy.deepcopy(canonical_authority())
        evaluation_contract = next(
            contract
            for contract in self.authority["trace_profiles"][
                "model_stage_producer_contracts"
            ]
            if contract["stage_id"] == "evaluation_review"
        )
        evaluation_tools = [
            {
                "type": "function",
                "function": {
                    "name": "submit_evaluation_review",
                    "parameters": {"type": "object"},
                },
            }
        ]
        evaluation_contract.update(
            {
                "producer_status": "test_double",
                "prompt_bundle_sha256": canonical_sha256(
                    {
                        "messages": [
                            {
                                "role": "system",
                                "content": "contract:evaluation_review",
                            }
                        ]
                    }
                ),
                "tool_bundle_sha256": canonical_sha256(
                    evaluation_tools
                ),
                "decoder_release_sha256": (
                    chat_completions._ADAPTER_RELEASE_SHA256
                ),
            }
        )
        self.manifest = execution_manifest(self.authority)

    def test_canonical_evaluation_reviewer_stays_unprovisioned(self) -> None:
        canonical = canonical_authority()
        contract = next(
            item
            for item in canonical["trace_profiles"][
                "model_stage_producer_contracts"
            ]
            if item["stage_id"] == "evaluation_review"
        )
        self.assertEqual(contract["producer_status"], "unprovisioned")
        canonical_manifest = execution_manifest(canonical)
        self.assertTrue(
            any(
                "unprovisioned model stages: evaluation_review" in finding
                for finding in validate_execution_manifest(
                    canonical_manifest,
                    authority=canonical,
                )
            )
        )

    def test_lane_and_model_stage_baselines_cannot_self_downgrade(self) -> None:
        canonical = canonical_authority()
        canonical_manifest = execution_manifest(canonical)
        profile_attack = copy.deepcopy(canonical)
        full_profile = next(
            profile
            for profile in profile_attack["trace_profiles"]["profiles"]
            if profile["lane"] == "full_authority"
        )
        full_profile["required_stage_ids"].remove("runtime_review")
        del full_profile["required_predecessors"]["runtime_review"]
        full_profile["required_predecessors"]["settlement_boundary"] = [
            "claim_proposal"
        ]
        self.assertTrue(
            any(
                "differs from the lane stage baseline" in finding
                for finding in validate_execution_manifest(
                    canonical_manifest,
                    authority=profile_attack,
                )
            )
        )

        duplicate_lane_attack = copy.deepcopy(canonical)
        malicious_profile = copy.deepcopy(
            next(
                profile
                for profile in duplicate_lane_attack["trace_profiles"][
                    "profiles"
                ]
                if profile["lane"] == "full_authority"
            )
        )
        malicious_profile["profile_id"] = "TRACE-FULL-MALICIOUS"
        malicious_profile["required_stage_ids"].remove("runtime_review")
        del malicious_profile["required_predecessors"]["runtime_review"]
        malicious_profile["required_predecessors"]["settlement_boundary"] = [
            "claim_proposal"
        ]
        duplicate_lane_attack["trace_profiles"]["profiles"].insert(
            0,
            malicious_profile,
        )
        self.assertIn(
            "trace profile registry contains duplicate lanes",
            validate_execution_manifest(
                canonical_manifest,
                authority=duplicate_lane_attack,
            ),
        )

        producer_attack = copy.deepcopy(canonical)
        producer_attack["trace_profiles"][
            "model_stage_producer_contracts"
        ] = [
            contract
            for contract in producer_attack["trace_profiles"][
                "model_stage_producer_contracts"
            ]
            if contract["stage_id"] not in {"runtime_review", "claim_proposal"}
        ]
        self.assertIn(
            "model stage producer registry differs from the runtime capability baseline",
            validate_execution_manifest(
                canonical_manifest,
                authority=producer_attack,
            ),
        )

        false_implementation = copy.deepcopy(canonical)
        evaluation_contract = next(
            contract
            for contract in false_implementation["trace_profiles"][
                "model_stage_producer_contracts"
            ]
            if contract["stage_id"] == "evaluation_review"
        )
        evaluation_contract["producer_status"] = "runtime_implemented"
        self.assertIn(
            "model stage producer registry differs from the runtime capability baseline",
            validate_execution_manifest(
                canonical_manifest,
                authority=false_implementation,
            ),
        )

        for field, value in (
            ("logical_job_kind", "no_runtime_producer"),
            (
                "typed_request_contract_ref",
                "waje-vnext://runtime/missing-job.v1",
            ),
            (
                "output_contract_ref",
                "waje-vnext://contracts/domain/missing.v1",
            ),
            ("prompt_bundle_sha256", "f" * 64),
        ):
            capability_attack = copy.deepcopy(canonical)
            typed_binding = next(
                contract
                for contract in capability_attack["trace_profiles"][
                    "model_stage_producer_contracts"
                ]
                if contract["stage_id"] == "typed_binding"
            )
            typed_binding[field] = value
            self.assertIn(
                "model stage producer registry differs from the runtime capability baseline",
                validate_execution_manifest(
                    canonical_manifest,
                    authority=capability_attack,
                ),
            )

    def test_full_lane_requires_claim_and_answer_review_model_jobs(self) -> None:
        cell = execution_cell(self.authority, episode_id="G3-USER-003")
        full_profile = next(
            profile
            for profile in self.authority["trace_profiles"]["profiles"]
            if profile["lane"] == "full_authority"
        )
        cell.update(
            {
                "lane": "full_authority",
                "trace_profile_ref": full_profile["profile_id"],
                "trace_profile_sha256": canonical_sha256(full_profile),
                "required_stage_ids": full_profile["required_stage_ids"],
            }
        )
        manifest = execution_manifest(self.authority, cells=[cell])
        bundle, artifact_index = trace_bundle(cell, manifest)
        executions = [
            runtime_model_execution(
                self.authority,
                cell,
                manifest,
                bundle,
                artifact_index,
                stage_id=stage_id,
            )
            for stage_id in (
                "typed_binding",
                "frame_proposal",
                "frame_review",
                "evaluation_review",
            )
        ]
        bind_executions_to_run_trace(executions, bundle)
        journal = attempt_journal(
            manifest,
            artifact_set_sha256=trace_artifact_set_sha256(artifact_index),
        )
        checks = hard_check_result(
            self.authority,
            manifest,
            cell,
            journal,
            bundle,
        )
        result = cell_result(
            manifest,
            journal,
            bundle,
            artifact_index,
            executions,
            checks,
        )
        findings = validate_cell_result(
            result,
            manifest=manifest,
            attempt_journal=journal,
            trace_bundle=bundle,
            trace_artifact_index=artifact_index,
            runtime_model_executions=executions,
            hard_check_result=checks,
            authority=self.authority,
        )
        self.assertTrue(
            any(
                "lacks stages: claim_proposal,runtime_review" in finding
                for finding in findings
            )
        )

    def test_development_manifest_binds_profiles_and_trace(self) -> None:
        self.assertEqual(
            validate_execution_manifest(
                self.manifest,
                authority=self.authority,
            ),
            TEST_DOUBLE_ADMISSION_FINDINGS,
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

    def test_canonical_typed_corpus_waits_for_evaluator_runtime(self) -> None:
        canonical = canonical_authority()
        manifest = execution_manifest(canonical)
        self.assertEqual(
            validate_execution_manifest(manifest, authority=canonical),
            [
                "execution manifest requires unprovisioned model stages: "
                "evaluation_review"
            ],
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
        attacked_manifest = copy.deepcopy(manifest)
        attacked_manifest["catalog_sha256"] = canonical_sha256(
            attacked_authority["catalog"]
        )
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

    def test_one_cell_can_participate_in_outcome_and_mutation_relations(
        self,
    ) -> None:
        anchor = execution_cell(self.authority, cell_id="CELL-ANCHOR-001")
        subject = counterfactual_cell(
            self.authority,
            cell_id="CELL-SUBJECT-001",
            sibling_index=1,
        )
        standalone = next(
            item
            for item in self.authority["mutation_operators"]["operators"]
            if item["operator_id"] == "episode_outcome"
        )
        mutation = next(
            item
            for item in self.authority["mutation_operators"]["operators"]
            if item["operator_id"] == "material_semantic_change"
        )
        groups = [
            {
                "relation_group_id": f"REL-{cell['execution_cell_id']}",
                "operator_ref": standalone["operator_id"],
                "operator_sha256": canonical_sha256(standalone),
                "expected_relation": standalone["expected_relation"],
                "scenario_binding": None,
                "members": [
                    {
                        "execution_cell_id": cell["execution_cell_id"],
                        "member_role": "singleton",
                    }
                ],
            }
            for cell in (anchor, subject)
        ]
        groups.append(
            {
                "relation_group_id": "REL-MATERIAL-CHANGE-001",
                "operator_ref": mutation["operator_id"],
                "operator_sha256": canonical_sha256(mutation),
                "expected_relation": mutation["expected_relation"],
                "scenario_binding": None,
                "members": [
                    {
                        "execution_cell_id": anchor["execution_cell_id"],
                        "member_role": "anchor",
                    },
                    {
                        "execution_cell_id": subject["execution_cell_id"],
                        "member_role": "subject",
                    },
                ],
            }
        )
        manifest = execution_manifest(
            self.authority,
            cells=[anchor, subject],
            relation_groups=groups,
        )
        self.assertEqual(
            validate_execution_manifest(manifest, authority=self.authority),
            TEST_DOUBLE_ADMISSION_FINDINGS,
        )

    def test_smoke_relation_cannot_relabel_a_sibling_operator(self) -> None:
        anchor = execution_cell(self.authority, cell_id="CELL-ANCHOR-ATTACK")
        subject = counterfactual_cell(
            self.authority,
            cell_id="CELL-SUBJECT-ATTACK",
            sibling_index=0,
        )
        standalone = next(
            item
            for item in self.authority["mutation_operators"]["operators"]
            if item["operator_id"] == "episode_outcome"
        )
        wrong_operator = next(
            item
            for item in self.authority["mutation_operators"]["operators"]
            if item["operator_id"] == "material_semantic_change"
        )
        groups = [
            {
                "relation_group_id": f"REL-{cell['execution_cell_id']}",
                "operator_ref": standalone["operator_id"],
                "operator_sha256": canonical_sha256(standalone),
                "expected_relation": standalone["expected_relation"],
                "scenario_binding": None,
                "members": [
                    {
                        "execution_cell_id": cell["execution_cell_id"],
                        "member_role": "singleton",
                    }
                ],
            }
            for cell in (anchor, subject)
        ]
        groups.append(
            {
                "relation_group_id": "REL-WRONG-SIBLING-OPERATOR",
                "operator_ref": wrong_operator["operator_id"],
                "operator_sha256": canonical_sha256(wrong_operator),
                "expected_relation": wrong_operator["expected_relation"],
                "scenario_binding": None,
                "members": [
                    {
                        "execution_cell_id": anchor["execution_cell_id"],
                        "member_role": "anchor",
                    },
                    {
                        "execution_cell_id": subject["execution_cell_id"],
                        "member_role": "subject",
                    },
                ],
            }
        )
        attacked = execution_manifest(
            self.authority,
            cells=[anchor, subject],
            relation_groups=groups,
        )
        self.assertTrue(
            any(
                "sibling relation operator drifted" in finding
                for finding in validate_execution_manifest(
                    attacked,
                    authority=self.authority,
                )
            )
        )
        correct_operator = next(
            item
            for item in self.authority["mutation_operators"]["operators"]
            if item["operator_id"] == "meaning_preserving_case_mutation"
        )
        crossed_repeat = copy.deepcopy(attacked)
        crossed_repeat["cells"][1]["repeat_index"] = 99
        crossed_repeat["relation_groups"][-1].update(
            {
                "operator_ref": correct_operator["operator_id"],
                "operator_sha256": canonical_sha256(correct_operator),
                "expected_relation": correct_operator[
                    "expected_relation"
                ],
            }
        )
        self.assertTrue(
            any(
                "mutation crosses repeat_index" in finding
                for finding in validate_execution_manifest(
                    crossed_repeat,
                    authority=self.authority,
                )
            )
        )

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
            TEST_DOUBLE_ADMISSION_FINDINGS,
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
            cells=[execution_cell(self.authority, episode_id="G3-USER-003")],
        )
        attacked["run_mode"] = "full"
        findings = validate_execution_manifest(
            attacked,
            authority=self.authority,
        )
        self.assertTrue(
            any("semantic_frame has too few paraphrases" in item for item in findings)
        )
        self.assertTrue(
            any("full_authority has too few paraphrases" in item for item in findings)
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

        future_event, future_event_index = trace_bundle(
            cell,
            self.manifest,
        )
        future_event["persisted_run_trace_manifest"][
            "event_operation_lineage"
        ][-1]["recorded_at"] = "2026-07-31T12:00:03+00:00"
        refresh_persisted_run_trace_manifest(
            future_event,
            future_event_index,
        )
        self.assertTrue(
            any(
                "persisted run trace manifest lineage is invalid" in finding
                for finding in validate_trace_bundle(
                    future_event,
                    cell,
                    manifest=self.manifest,
                    artifact_index=future_event_index,
                    authority=self.authority,
                )
            )
        )

    def test_business_stage_cannot_bind_an_unrelated_same_run_event(self) -> None:
        cell = self.manifest["cells"][0]
        bundle, artifact_index = trace_bundle(cell, self.manifest)
        message_stage = next(
            stage
            for stage in bundle["stages"]
            if stage["stage_id"] == "message_ingress"
        )
        wrong_event = next(
            event
            for event in bundle["persisted_run_trace_manifest"][
                "event_operation_lineage"
            ]
            if event["event_type"] == "frame_accepted"
        )
        record = next(
            item
            for item in artifact_index["records"]
            if item["artifact_ref"] == message_stage["artifact_ref"]
        )
        record["authority_source_ref"] = wrong_event["event_id"]
        record["journal_cursor"] = wrong_event["cursor"]
        record["artifact_sha256"] = wrong_event["event_content_sha256"]
        message_stage["journal_cursor"] = wrong_event["cursor"]
        message_stage["artifact_sha256"] = wrong_event[
            "event_content_sha256"
        ]
        self.assertTrue(
            any(
                "uses the wrong journal event type" in finding
                for finding in validate_trace_bundle(
                    bundle,
                    cell,
                    manifest=self.manifest,
                    artifact_index=artifact_index,
                    authority=self.authority,
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
        runtime_executions = runtime_model_executions(
            self.authority,
            cell,
            self.manifest,
            bundle,
            artifact_index,
        )
        journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(artifact_index),
        )
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
            runtime_executions,
            hard_checks,
        )
        self.assertEqual(
            validate_cell_result(
                result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=runtime_executions,
                hard_check_result=hard_checks,
                authority=self.authority,
            ),
            [],
        )
        loop_index = copy.deepcopy(artifact_index)
        loop_bundle = copy.deepcopy(bundle)
        loop_executions = copy.deepcopy(runtime_executions)
        loop_executions.append(
            runtime_model_execution(
                self.authority,
                cell,
                self.manifest,
                loop_bundle,
                loop_index,
                stage_id="frame_proposal",
                suffix="followup",
                artifact_ref="artifact://frame_proposal/followup",
            )
        )
        bind_executions_to_run_trace(loop_executions, loop_bundle)
        loop_journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(loop_index),
        )
        loop_checks = hard_check_result(
            self.authority,
            self.manifest,
            cell,
            loop_journal,
            loop_bundle,
        )
        loop_result = cell_result(
            self.manifest,
            loop_journal,
            loop_bundle,
            loop_index,
            loop_executions,
            loop_checks,
        )
        self.assertEqual(
            validate_cell_result(
                loop_result,
                manifest=self.manifest,
                attempt_journal=loop_journal,
                trace_bundle=loop_bundle,
                trace_artifact_index=loop_index,
                runtime_model_executions=loop_executions,
                hard_check_result=loop_checks,
                authority=self.authority,
            ),
            [],
        )
        omitted_loop_execution = loop_executions[:-1]
        omitted_loop_result = copy.deepcopy(loop_result)
        omitted_loop_result["runtime_model_execution_set_sha256"] = (
            canonical_sha256(omitted_loop_execution)
        )
        self.assertTrue(
            any(
                "jobs differ from the persisted run trace" in finding
                for finding in validate_cell_result(
                    omitted_loop_result,
                    manifest=self.manifest,
                    attempt_journal=loop_journal,
                    trace_bundle=loop_bundle,
                    trace_artifact_index=loop_index,
                    runtime_model_executions=omitted_loop_execution,
                    hard_check_result=loop_checks,
                    authority=self.authority,
                )
            )
        )

        request_omission_bundle = copy.deepcopy(bundle)
        request_omission_index = copy.deepcopy(artifact_index)
        request_omission_executions = copy.deepcopy(runtime_executions)
        request_omission_bundle["persisted_run_trace_manifest"][
            "provider_attempt_request_ids"
        ].pop()
        refresh_persisted_run_trace_manifest(
            request_omission_bundle,
            request_omission_index,
        )
        bind_executions_to_run_trace(
            request_omission_executions,
            request_omission_bundle,
        )
        request_omission_journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(
                request_omission_index
            ),
        )
        request_omission_checks = hard_check_result(
            self.authority,
            self.manifest,
            cell,
            request_omission_journal,
            request_omission_bundle,
        )
        request_omission_result = cell_result(
            self.manifest,
            request_omission_journal,
            request_omission_bundle,
            request_omission_index,
            request_omission_executions,
            request_omission_checks,
        )
        self.assertTrue(
            any(
                "provider requests differ from the persisted run trace"
                in finding
                for finding in validate_cell_result(
                    request_omission_result,
                    manifest=self.manifest,
                    attempt_journal=request_omission_journal,
                    trace_bundle=request_omission_bundle,
                    trace_artifact_index=request_omission_index,
                    runtime_model_executions=request_omission_executions,
                    hard_check_result=request_omission_checks,
                    authority=self.authority,
                )
            )
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
                    runtime_model_executions=runtime_executions,
                    hard_check_result=forged_root_checks,
                    authority=self.authority,
                )
            )
        )
        forged_executions = copy.deepcopy(runtime_executions)
        forged_executions[0]["logical_model_job"]["configuration_identity"][
            "configuration_sha256"
        ] = "0" * 64
        forged = copy.deepcopy(result)
        forged["runtime_model_execution_set_sha256"] = canonical_sha256(
            forged_executions
        )
        self.assertTrue(
            any(
                "configuration hash drifted" in item
                for item in validate_cell_result(
                    forged,
                    manifest=self.manifest,
                    attempt_journal=journal,
                    trace_bundle=bundle,
                    trace_artifact_index=artifact_index,
                    runtime_model_executions=forged_executions,
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
                    runtime_model_executions=runtime_executions,
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
                attempt_journal=loop_journal,
                trace_bundle=bundle,
                trace_artifact_index=loop_index,
                runtime_model_executions=loop_executions,
                hard_check_result=loop_checks,
                authority=self.authority,
            ),
        )

    def test_runtime_execution_rejects_self_reported_and_cross_chain_facts(
        self,
    ) -> None:
        (
            _,
            bundle,
            artifact_index,
            executions,
            journal,
            hard_checks,
            result,
        ) = complete_cell_artifacts(self.authority, self.manifest)

        def findings_for(attacked):
            attacked_result = copy.deepcopy(result)
            attacked_result["runtime_model_execution_set_sha256"] = (
                canonical_sha256(attacked)
            )
            return validate_cell_result(
                attacked_result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=attacked,
                hard_check_result=hard_checks,
                authority=self.authority,
            )

        old_projection = [
            {
                "artifact_type": "gate3_model_invocation",
                "logical_model_job_id": "self-reported-job",
                "request_sha256": "4" * 64,
            }
        ]
        self.assertTrue(findings_for(old_projection))

        crossed = copy.deepcopy(executions)
        crossed[0]["attempts"][0]["receipt"][
            "logical_model_job_id"
        ] = crossed[1]["logical_model_job"]["logical_model_job_id"]
        crossed[0]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(crossed[0])
        )
        self.assertTrue(
            any(
                "receipt linkage drifted" in finding
                for finding in findings_for(crossed)
            )
        )

        missing_result_pair = copy.deepcopy(executions)
        missing_result_pair[0]["durable_result"][
            "provider_attempt_receipt_id"
        ] = "PROVIDER-RECEIPT-OTHER"
        missing_result_pair[0]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(missing_result_pair[0])
        )
        self.assertTrue(
            any(
                "provider_attempt_receipt_id drifted" in finding
                for finding in findings_for(missing_result_pair)
            )
        )

    def test_runtime_execution_rejects_rehashed_seed_stage_and_contract_drift(
        self,
    ) -> None:
        (
            _,
            bundle,
            artifact_index,
            executions,
            journal,
            hard_checks,
            result,
        ) = complete_cell_artifacts(self.authority, self.manifest)

        def findings_for(attacked):
            attacked_result = copy.deepcopy(result)
            attacked_result["runtime_model_execution_set_sha256"] = (
                canonical_sha256(attacked)
            )
            return validate_cell_result(
                attacked_result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=attacked,
                hard_check_result=hard_checks,
                authority=self.authority,
            )

        seed_drift = copy.deepcopy(executions)
        configuration = seed_drift[0]["logical_model_job"][
            "configuration_identity"
        ]
        configuration["stable_parameters"]["seed"] += 1
        seed_drift[0]["logical_model_job"]["model_request_artifact"][
            "provider_request_body"
        ]["seed"] += 1
        rehash_runtime_model_execution(seed_drift[0])
        self.assertTrue(
            any(
                "execution seed drifted" in finding
                for finding in findings_for(seed_drift)
            )
        )

        wrong_stage = copy.deepcopy(executions)
        frame_proposal = next(
            execution
            for execution in wrong_stage
            if execution["trace_output_binding"]["stage_id"]
            == "frame_proposal"
        )
        frame_proposal["trace_output_binding"]["stage_id"] = "frame_review"
        frame_proposal["trace_output_binding"]["artifact_ref"] = (
            "artifact://frame_review"
        )
        self.assertTrue(
            any(
                "stage producer" in finding
                or "profile" in finding
                or "trace output authority_source_ref drifted" in finding
                for finding in findings_for(wrong_stage)
            )
        )

        contract_drift = copy.deepcopy(executions)
        target = contract_drift[0]
        target["logical_model_job"]["model_request_artifact"][
            "output_contract_ref"
        ] = "waje-vnext://contracts/domain/wrong-output.v1"
        rehash_runtime_model_execution(target)
        self.assertTrue(
            any(
                "stage producer output_contract_ref drifted" in finding
                for finding in findings_for(contract_drift)
            )
        )

        unverified_source = copy.deepcopy(executions)
        unverified_source[0]["source_proof"]["mode"] = "direct_store_read"
        self.assertTrue(
            any(
                "lacks protected verification" in finding
                for finding in findings_for(unverified_source)
            )
        )

        configuration_drift = copy.deepcopy(executions)
        target = configuration_drift[0]
        target["logical_model_job"]["configuration_identity"][
            "stable_parameters"
        ]["temperature"] = 0.77
        target["logical_model_job"]["model_request_artifact"][
            "provider_request_body"
        ]["temperature"] = 0.77
        rehash_runtime_model_execution(target)
        self.assertTrue(
            any(
                "differs from the execution cell" in finding
                for finding in findings_for(configuration_drift)
            )
        )

        prompt_drift = copy.deepcopy(executions)
        target = prompt_drift[0]
        target["logical_model_job"]["model_request_artifact"][
            "provider_request_body"
        ]["messages"][0]["content"] += "\nUse hidden evaluator facts."
        rehash_runtime_model_execution(target)
        self.assertTrue(
            any(
                "stage producer prompt_bundle_sha256 drifted" in finding
                for finding in findings_for(prompt_drift)
            )
        )

        malformed_typed_request = copy.deepcopy(executions)
        target = malformed_typed_request[0]
        artifact = target["logical_model_job"]["model_request_artifact"]
        typed_request = json.loads(
            artifact["provider_request_body"]["messages"][1]["content"]
        )
        typed_request["unexpected_evaluator_hint"] = "hidden answer"
        artifact["provider_request_body"]["messages"][1]["content"] = (
            json.dumps(
                typed_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        rehash_runtime_model_execution(target)
        self.assertTrue(
            any(
                "cannot be replayed through the production invocation compiler"
                in finding
                for finding in findings_for(malformed_typed_request)
            )
        )

        stale_snapshot = copy.deepcopy(executions)
        stale_snapshot[0]["logical_model_job"][
            "authority_snapshot_sha256"
        ] = "9" * 64
        stale_snapshot[0]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(stale_snapshot[0])
        )
        self.assertTrue(
            any(
                "authority snapshot is absent" in finding
                or "authority_snapshot_sha256 drifted" in finding
                for finding in findings_for(stale_snapshot)
            )
        )

    def test_runtime_execution_rejects_invalid_typed_output_and_global_ids(
        self,
    ) -> None:
        (
            _,
            bundle,
            artifact_index,
            executions,
            journal,
            hard_checks,
            result,
        ) = complete_cell_artifacts(self.authority, self.manifest)

        def findings_for(attacked):
            attacked_result = copy.deepcopy(result)
            attacked_result["runtime_model_execution_set_sha256"] = (
                canonical_sha256(attacked)
            )
            return validate_cell_result(
                attacked_result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=attacked,
                hard_check_result=hard_checks,
                authority=self.authority,
            )

        wrong_action = copy.deepcopy(executions)
        proposal = next(
            execution
            for execution in wrong_action
            if execution["trace_output_binding"]["stage_id"]
            == "frame_proposal"
        )
        proposal["durable_result"]["result_payload"] = {
            "kind": "ask_user",
            "payload": {},
        }
        rehash_runtime_model_execution(proposal)
        self.assertTrue(
            any(
                "typed output contract" in finding
                or "wrong action stage" in finding
                for finding in findings_for(wrong_action)
            )
        )

        duplicate_receipt = copy.deepcopy(executions)
        shared_receipt_id = duplicate_receipt[0]["attempts"][0][
            "receipt"
        ]["provider_attempt_receipt_id"]
        duplicate_receipt[1]["attempts"][0]["receipt"][
            "provider_attempt_receipt_id"
        ] = shared_receipt_id
        duplicate_receipt[1]["durable_result"][
            "provider_attempt_receipt_id"
        ] = shared_receipt_id
        duplicate_receipt[1]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(duplicate_receipt[1])
        )
        self.assertTrue(
            any(
                "provider receipt ids must be unique" in finding
                for finding in findings_for(duplicate_receipt)
            )
        )

        duplicate_idempotency = copy.deepcopy(executions)
        duplicate_idempotency[1]["attempts"][0]["request"][
            "provider_idempotency_key"
        ] = duplicate_idempotency[0]["attempts"][0]["request"][
            "provider_idempotency_key"
        ]
        duplicate_idempotency[1]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(duplicate_idempotency[1])
        )
        self.assertTrue(
            any(
                "provider idempotency keys must be unique" in finding
                for finding in findings_for(duplicate_idempotency)
            )
        )

        wrong_message_ingress = copy.deepcopy(executions)
        binding = next(
            execution
            for execution in wrong_message_ingress
            if execution["trace_output_binding"]["stage_id"]
            == "typed_binding"
        )
        artifact = binding["logical_model_job"]["model_request_artifact"]
        typed_request = json.loads(
            artifact["provider_request_body"]["messages"][1]["content"]
        )
        typed_request["message_id"] = "CASE-G36-001:message:other"
        artifact["provider_request_body"]["messages"][1]["content"] = (
            json.dumps(
                typed_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        artifact["input_view_ref"] = typed_request["message_id"]
        artifact["input_view_sha256"] = canonical_sha256(typed_request)
        rehash_runtime_model_execution(binding)
        self.assertTrue(
            any(
                "typed binding input does not match" in finding
                for finding in findings_for(wrong_message_ingress)
            )
        )

        self.assertEqual(
            set(
                _runtime_model_global_identity_findings(
                    {
                        "CELL-A": [executions[0]],
                        "CELL-B": [copy.deepcopy(executions[0])],
                    }
                )
            ),
            {
                "run trace manifest ids must be globally unique across cells",
                "logical model job ids must be globally unique across cells",
                "provider attempt ids must be globally unique across cells",
                "provider receipt ids must be globally unique across cells",
                "provider idempotency key ids must be globally unique across cells",
                "durable model result ids must be globally unique across cells",
            },
        )

        cross_case_candidate = copy.deepcopy(executions)
        review = next(
            execution
            for execution in cross_case_candidate
            if execution["trace_output_binding"]["stage_id"]
            == "frame_review"
        )
        artifact = review["logical_model_job"]["model_request_artifact"]
        typed_request = json.loads(
            artifact["provider_request_body"]["messages"][1]["content"]
        )
        typed_request["frame_candidate"]["frame_candidate_id"] = (
            "FRAME-FROM-ANOTHER-CASE"
        )
        artifact["provider_request_body"]["messages"][1]["content"] = (
            json.dumps(
                typed_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        artifact["input_view_ref"] = "FRAME-FROM-ANOTHER-CASE"
        artifact["input_view_sha256"] = canonical_sha256(typed_request)
        rehash_runtime_model_execution(review)
        self.assertTrue(
            any(
                "frame candidate is absent from the persisted run trace"
                in finding
                for finding in findings_for(cross_case_candidate)
            )
        )

        wrong_same_run_candidate = copy.deepcopy(executions)
        review = next(
            execution
            for execution in wrong_same_run_candidate
            if execution["trace_output_binding"]["stage_id"]
            == "frame_review"
        )
        artifact = review["logical_model_job"]["model_request_artifact"]
        typed_request = json.loads(
            artifact["provider_request_body"]["messages"][1]["content"]
        )
        proposed_frame = typed_request["frame_candidate"]["proposed_frame"]
        proposed_frame["measurement_design"]["window_rules"][0][
            "selection_count"
        ] += 1
        typed_request["frame_candidate"][
            "proposed_frame_content_sha256"
        ] = canonical_sha256(proposed_frame)
        artifact["provider_request_body"]["messages"][1]["content"] = (
            json.dumps(
                typed_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        artifact["input_view_sha256"] = canonical_sha256(typed_request)
        rehash_runtime_model_execution(review)
        self.assertTrue(
            any(
                "do not share one frame authority" in finding
                for finding in findings_for(wrong_same_run_candidate)
            )
        )

        forged_frame_identity = copy.deepcopy(executions)
        review = next(
            execution
            for execution in forged_frame_identity
            if execution["trace_output_binding"]["stage_id"]
            == "frame_review"
        )
        artifact = review["logical_model_job"]["model_request_artifact"]
        typed_request = json.loads(
            artifact["provider_request_body"]["messages"][1]["content"]
        )
        proposed_frame = typed_request["frame_candidate"]["proposed_frame"]
        proposed_frame["authority_binding_ids"][0] = "e" * 64
        typed_request["frame_candidate"][
            "proposed_frame_content_sha256"
        ] = canonical_sha256(proposed_frame)
        artifact["provider_request_body"]["messages"][1]["content"] = (
            json.dumps(
                typed_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        artifact["input_view_sha256"] = canonical_sha256(typed_request)
        rehash_runtime_model_execution(review)
        self.assertTrue(
            any(
                "do not share one frame authority" in finding
                for finding in findings_for(forged_frame_identity)
            )
        )

        conflicting_review = copy.deepcopy(executions)
        conflicting_bundle = copy.deepcopy(bundle)
        conflicting_index = copy.deepcopy(artifact_index)
        review = next(
            execution
            for execution in conflicting_review
            if execution["trace_output_binding"]["stage_id"]
            == "frame_review"
        )
        review["durable_result"]["result_payload"] = {
            "disposition": "revise",
            "objections": [
                {
                    "code": "comparison_boundary_needs_revision",
                    "severity": "material",
                    "affected_node_refs": [
                        "measurement_design.window_rules.0"
                    ],
                    "explanation": "The proposed boundary needs revision.",
                }
            ],
            "review_summary": "Revise the material comparison boundary.",
        }
        rehash_runtime_model_execution(review)
        output_sha256 = review["durable_result"]["output_sha256"]
        review_stage = next(
            stage
            for stage in conflicting_bundle["stages"]
            if stage["stage_id"] == "frame_review"
        )
        review_stage["artifact_sha256"] = output_sha256
        review_record = next(
            record
            for record in conflicting_index["records"]
            if record["artifact_ref"] == review_stage["artifact_ref"]
        )
        review_record["artifact_sha256"] = output_sha256
        conflicting_journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(
                conflicting_index
            ),
        )
        conflicting_checks = hard_check_result(
            self.authority,
            self.manifest,
            self.manifest["cells"][0],
            conflicting_journal,
            conflicting_bundle,
        )
        conflicting_result = cell_result(
            self.manifest,
            conflicting_journal,
            conflicting_bundle,
            conflicting_index,
            conflicting_review,
            conflicting_checks,
        )
        self.assertTrue(
            any(
                "non-accepting frame review conflicts" in finding
                for finding in validate_cell_result(
                    conflicting_result,
                    manifest=self.manifest,
                    attempt_journal=conflicting_journal,
                    trace_bundle=conflicting_bundle,
                    trace_artifact_index=conflicting_index,
                    runtime_model_executions=conflicting_review,
                    hard_check_result=conflicting_checks,
                    authority=self.authority,
                )
            )
        )

    def test_trace_validation_is_fail_closed_and_rejects_unowned_records(
        self,
    ) -> None:
        (
            _,
            bundle,
            artifact_index,
            executions,
            journal,
            hard_checks,
            result,
        ) = complete_cell_artifacts(self.authority, self.manifest)

        malformed_index = copy.deepcopy(artifact_index)
        malformed_index["records"] = [{}]
        malformed_result = copy.deepcopy(result)
        malformed_result["trace_artifact_index_sha256"] = canonical_sha256(
            malformed_index
        )
        malformed_findings = validate_cell_result(
            malformed_result,
            manifest=self.manifest,
            attempt_journal=journal,
            trace_bundle=bundle,
            trace_artifact_index=malformed_index,
            runtime_model_executions=executions,
            hard_check_result=hard_checks,
            authority=self.authority,
        )
        self.assertTrue(malformed_findings)
        self.assertTrue(
            any(
                "verified trace artifacts" in finding
                for finding in malformed_findings
            )
        )

        malformed_manifest = copy.deepcopy(self.manifest)
        del malformed_manifest["cells"]
        self.assertTrue(
            validate_cell_result(
                result,
                manifest=malformed_manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=executions,
                hard_check_result=hard_checks,
                authority=self.authority,
            )
        )

        malformed_journal = copy.deepcopy(journal)
        malformed_journal["cell_attempts"] = [{}]
        self.assertTrue(
            validate_cell_result(
                result,
                manifest=self.manifest,
                attempt_journal=malformed_journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=executions,
                hard_check_result=hard_checks,
                authority=self.authority,
            )
        )

        malformed_suite = derive_suite_result(
            malformed_manifest,
            [result],
            attempt_journal=malformed_journal,
            trace_bundles={result["execution_cell_id"]: bundle},
            trace_artifact_indexes={
                result["execution_cell_id"]: artifact_index
            },
            runtime_model_executions_by_cell={
                result["execution_cell_id"]: executions
            },
            hard_check_results={
                result["execution_cell_id"]: hard_checks
            },
            authority=self.authority,
        )
        self.assertEqual(
            malformed_suite["local_execution_status"],
            "invalid",
        )
        for malformed_top_level in ([], None, "bad"):
            malformed_suite = derive_suite_result(
                malformed_top_level,
                [],
                authority=self.authority,
            )
            self.assertEqual(
                malformed_suite["local_execution_status"],
                "invalid",
            )
        for malformed_map_name in (
            "trace_bundles",
            "trace_artifact_indexes",
            "runtime_model_executions_by_cell",
            "hard_check_results",
        ):
            malformed_suite = derive_suite_result(
                self.manifest,
                [],
                authority=self.authority,
                **{malformed_map_name: [{}]},
            )
            self.assertEqual(
                malformed_suite["local_execution_status"],
                "invalid",
            )
        for malformed_results in (None, 7):
            malformed_suite = derive_suite_result(
                self.manifest,
                malformed_results,
                authority=self.authority,
            )
            self.assertEqual(
                malformed_suite["local_execution_status"],
                "invalid",
            )
        self.assertEqual(
            derive_suite_result(
                self.manifest,
                [],
                relation_results=None,
                authority=self.authority,
            )["local_execution_status"],
            "invalid",
        )
        self.assertEqual(
            derive_suite_result(
                self.manifest,
                [],
                manifest_findings=None,
                authority=self.authority,
            )["local_execution_status"],
            "invalid",
        )
        self.assertEqual(
            derive_suite_result(
                self.manifest,
                [],
                authority=[{}],
            )["local_execution_status"],
            "invalid",
        )
        for malformed_authority in ({}, {"policy": {}}, {"bad": "x"}):
            self.assertEqual(
                validate_execution_manifest(
                    self.manifest,
                    authority=malformed_authority,
                ),
                ["evaluation authority structure is invalid"],
            )
            self.assertEqual(
                derive_suite_result(
                    self.manifest,
                    [],
                    authority=malformed_authority,
                )["local_execution_status"],
                "invalid",
            )
        self.assertEqual(
            set(
                _cell_artifact_map_key_findings(
                    {"CELL-DEV-001"},
                    {
                        "trace bundle": {
                            "CELL-DEV-001": bundle,
                            "CELL-GHOST": {},
                        },
                        "trace artifact index": {
                            "CELL-DEV-001": artifact_index,
                        },
                    },
                )
            ),
            {"trace bundle cell keys differ from cell results"},
        )

        extra_index = copy.deepcopy(artifact_index)
        extra_record = copy.deepcopy(extra_index["records"][0])
        extra_record.update(
            {
                "artifact_ref": "artifact://unowned-authority-record",
                "artifact_sha256": "d" * 64,
                "artifact_kind": "authority_record",
                "authority_source_kind": "event_journal",
                "authority_source_ref": "EVENT-DOES-NOT-EXIST",
            }
        )
        extra_index["records"].append(extra_record)
        extra_journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(extra_index),
        )
        extra_checks = hard_check_result(
            self.authority,
            self.manifest,
            self.manifest["cells"][0],
            extra_journal,
            bundle,
        )
        extra_result = cell_result(
            self.manifest,
            extra_journal,
            bundle,
            extra_index,
            executions,
            extra_checks,
        )
        self.assertTrue(
            any(
                "execution authority closure" in finding
                for finding in validate_cell_result(
                    extra_result,
                    manifest=self.manifest,
                    attempt_journal=extra_journal,
                    trace_bundle=bundle,
                    trace_artifact_index=extra_index,
                    runtime_model_executions=executions,
                    hard_check_result=extra_checks,
                    authority=self.authority,
                )
            )
        )

        forged_event_index = copy.deepcopy(artifact_index)
        frame_disposition = next(
            record
            for record in forged_event_index["records"]
            if record["artifact_ref"] == "artifact://frame_disposition"
        )
        frame_disposition["authority_source_ref"] = (
            "EVENT-DOES-NOT-EXIST"
        )
        forged_event_journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(
                forged_event_index
            ),
        )
        forged_event_checks = hard_check_result(
            self.authority,
            self.manifest,
            self.manifest["cells"][0],
            forged_event_journal,
            bundle,
        )
        forged_event_result = cell_result(
            self.manifest,
            forged_event_journal,
            bundle,
            forged_event_index,
            executions,
            forged_event_checks,
        )
        self.assertTrue(
            any(
                "references an unknown journal event" in finding
                for finding in validate_cell_result(
                    forged_event_result,
                    manifest=self.manifest,
                    attempt_journal=forged_event_journal,
                    trace_bundle=bundle,
                    trace_artifact_index=forged_event_index,
                    runtime_model_executions=executions,
                    hard_check_result=forged_event_checks,
                    authority=self.authority,
                )
            )
        )

    def test_runtime_execution_rejects_invalid_and_nonmonotonic_timestamps(
        self,
    ) -> None:
        (
            _,
            bundle,
            artifact_index,
            executions,
            journal,
            hard_checks,
            result,
        ) = complete_cell_artifacts(self.authority, self.manifest)

        def findings_for(attacked):
            attacked_result = copy.deepcopy(result)
            attacked_result["runtime_model_execution_set_sha256"] = (
                canonical_sha256(attacked)
            )
            return validate_cell_result(
                attacked_result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=attacked,
                hard_check_result=hard_checks,
                authority=self.authority,
            )

        malformed = copy.deepcopy(executions)
        malformed[0]["logical_model_job"]["created_at"] = "garbage"
        malformed[0]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(malformed[0])
        )
        self.assertTrue(findings_for(malformed))

        reversed_time = copy.deepcopy(executions)
        reversed_time[0]["attempts"][0]["receipt"]["completed_at"] = (
            "2026-07-31T11:59:59+00:00"
        )
        reversed_time[0]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(reversed_time[0])
        )
        self.assertTrue(
            any(
                "completes before request" in finding
                for finding in findings_for(reversed_time)
            )
        )

    def test_runtime_execution_validates_retry_history_without_role_limits(
        self,
    ) -> None:
        (
            _,
            bundle,
            artifact_index,
            executions,
            journal,
            hard_checks,
            result,
        ) = complete_cell_artifacts(self.authority, self.manifest)

        retried = copy.deepcopy(executions)
        target = retried[0]
        success = copy.deepcopy(target["attempts"][0])
        first = target["attempts"][0]
        first["receipt"]["disposition"] = "retryable_failure"
        first["receipt"]["provider_response_id"] = None
        first["receipt"]["output_sha256"] = None
        first["receipt"]["finish_reason"] = "provider_timeout"
        success["request"]["provider_attempt_id"] += "-retry"
        success["request"]["attempt_number"] = 2
        success["request"]["prior_provider_attempt_id"] = first[
            "request"
        ]["provider_attempt_id"]
        success["request"]["provider_idempotency_key"] += "-retry"
        success["request"]["requested_at"] = (
            "2026-07-31T12:00:01.100000+00:00"
        )
        success["receipt"]["provider_attempt_receipt_id"] += "-retry"
        success["receipt"]["provider_attempt_id"] = success["request"][
            "provider_attempt_id"
        ]
        success["receipt"]["completed_at"] = (
            "2026-07-31T12:00:01.900000+00:00"
        )
        target["durable_result"]["recorded_at"] = (
            "2026-07-31T12:00:01.900000+00:00"
        )
        target["attempts"] = [first, success]
        rehash_runtime_model_execution(target)
        retried_bundle = copy.deepcopy(bundle)
        retried_index = copy.deepcopy(artifact_index)
        retried_bundle["persisted_run_trace_manifest"][
            "provider_attempt_request_ids"
        ].append(success["request"]["provider_attempt_id"])
        retried_bundle["persisted_run_trace_manifest"][
            "provider_attempt_receipt_ids"
        ].append(success["receipt"]["provider_attempt_receipt_id"])
        refresh_persisted_run_trace_manifest(retried_bundle, retried_index)
        bind_executions_to_run_trace(retried, retried_bundle)
        retried_journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(retried_index),
        )
        retried_checks = hard_check_result(
            self.authority,
            self.manifest,
            self.manifest["cells"][0],
            retried_journal,
            retried_bundle,
        )
        retried_result = cell_result(
            self.manifest,
            retried_journal,
            retried_bundle,
            retried_index,
            retried,
            retried_checks,
        )
        self.assertEqual(
            validate_cell_result(
                retried_result,
                manifest=self.manifest,
                attempt_journal=retried_journal,
                trace_bundle=retried_bundle,
                trace_artifact_index=retried_index,
                runtime_model_executions=retried,
                hard_check_result=retried_checks,
                authority=self.authority,
            ),
            [],
        )

        post_snapshot = copy.deepcopy(retried)
        post_snapshot[0]["logical_model_job"]["created_at"] = (
            "2026-07-31T12:00:03+00:00"
        )
        post_snapshot[0]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(post_snapshot[0])
        )
        attacked_result = copy.deepcopy(retried_result)
        attacked_result["runtime_model_execution_set_sha256"] = (
            canonical_sha256(post_snapshot)
        )
        self.assertTrue(
            any(
                "postdates its run trace manifest" in finding
                for finding in validate_cell_result(
                    attacked_result,
                    manifest=self.manifest,
                    attempt_journal=retried_journal,
                    trace_bundle=retried_bundle,
                    trace_artifact_index=retried_index,
                    runtime_model_executions=post_snapshot,
                    hard_check_result=retried_checks,
                    authority=self.authority,
                )
            )
        )

        continued_after_terminal = copy.deepcopy(retried)
        continued_after_terminal[0]["attempts"][0]["receipt"][
            "disposition"
        ] = "terminal_failure"
        continued_after_terminal[0]["runtime_record_set_sha256"] = (
            runtime_model_record_set_sha256(continued_after_terminal[0])
        )
        attacked_result = copy.deepcopy(retried_result)
        attacked_result["runtime_model_execution_set_sha256"] = canonical_sha256(
            continued_after_terminal
        )
        self.assertTrue(
            any(
                "continues after a terminal receipt" in finding
                for finding in validate_cell_result(
                    attacked_result,
                    manifest=self.manifest,
                    attempt_journal=retried_journal,
                    trace_bundle=retried_bundle,
                    trace_artifact_index=retried_index,
                    runtime_model_executions=continued_after_terminal,
                    hard_check_result=retried_checks,
                    authority=self.authority,
                )
            )
        )

    def test_runtime_projection_reads_the_exact_store_record_set(self) -> None:
        (
            _,
            bundle,
            artifact_index,
            executions,
            journal,
            hard_checks,
            result,
        ) = complete_cell_artifacts(self.authority, self.manifest)
        source = executions[0]
        job = decode_logical_model_job(source["logical_model_job"])
        requests = tuple(
            decode_provider_attempt_request(item["request"])
            for item in source["attempts"]
        )
        receipts = tuple(
            decode_provider_attempt_receipt(item["receipt"])
            for item in source["attempts"]
        )
        durable_result = decode_durable_model_result(
            source["durable_result"]
        )
        run_trace_manifest = decode_run_trace_manifest(
            bundle["persisted_run_trace_manifest"]
        )

        class SnapshotStore:
            def __init__(self, request_records=requests, result_record=durable_result):
                self.request_records = request_records
                self.result_record = result_record

            def read_model_execution_trace_records(
                self,
                logical_model_job_id,
                trace_manifest_id,
            ):
                self.logical_model_job_id = logical_model_job_id
                self.trace_manifest_id = trace_manifest_id
                return (
                    job,
                    self.request_records,
                    receipts,
                    self.result_record,
                    run_trace_manifest,
                )

        projected = project_runtime_model_execution(
            SnapshotStore(),
            logical_model_job_id=job.logical_model_job_id,
            run_trace_manifest_id=run_trace_manifest.trace_manifest_id,
            execution_manifest_sha256=canonical_sha256(self.manifest),
            execution_cell_id=result["execution_cell_id"],
            execution_attempt_id=result["terminal_attempt_id"],
            evaluator_profile_ref=source["evaluator_profile_ref"],
            evaluator_profile_sha256=source["evaluator_profile_sha256"],
            trace_stage_id=source["trace_output_binding"]["stage_id"],
            trace_artifact_ref=source["trace_output_binding"]["artifact_ref"],
            runtime_store_ref="postgres://snapshot-reader",
            snapshot_ref="snapshot://repeatable-read-1",
        )
        projected_set = [projected, *copy.deepcopy(executions[1:])]
        projected_result = copy.deepcopy(result)
        projected_result["runtime_model_execution_set_sha256"] = canonical_sha256(
            projected_set
        )
        self.assertEqual(
            validate_cell_result(
                projected_result,
                manifest=self.manifest,
                attempt_journal=journal,
                trace_bundle=bundle,
                trace_artifact_index=artifact_index,
                runtime_model_executions=projected_set,
                hard_check_result=hard_checks,
                authority=self.authority,
            ),
            [],
        )
        with self.assertRaises(RuntimeProjectionError):
            project_runtime_model_execution(
                SnapshotStore(request_records=requests + requests),
                logical_model_job_id=job.logical_model_job_id,
                run_trace_manifest_id=run_trace_manifest.trace_manifest_id,
                execution_manifest_sha256=canonical_sha256(self.manifest),
                execution_cell_id=result["execution_cell_id"],
                execution_attempt_id=result["terminal_attempt_id"],
                evaluator_profile_ref=source["evaluator_profile_ref"],
                evaluator_profile_sha256=source["evaluator_profile_sha256"],
                trace_stage_id=source["trace_output_binding"]["stage_id"],
                trace_artifact_ref=source["trace_output_binding"]["artifact_ref"],
                runtime_store_ref="postgres://snapshot-reader",
                snapshot_ref="snapshot://repeatable-read-2",
            )
        with self.assertRaises(RuntimeProjectionError):
            project_runtime_model_execution(
                SnapshotStore(result_record=None),
                logical_model_job_id=job.logical_model_job_id,
                run_trace_manifest_id=run_trace_manifest.trace_manifest_id,
                execution_manifest_sha256=canonical_sha256(self.manifest),
                execution_cell_id=result["execution_cell_id"],
                execution_attempt_id=result["terminal_attempt_id"],
                evaluator_profile_ref=source["evaluator_profile_ref"],
                evaluator_profile_sha256=source["evaluator_profile_sha256"],
                trace_stage_id=source["trace_output_binding"]["stage_id"],
                trace_artifact_ref=source["trace_output_binding"]["artifact_ref"],
                runtime_store_ref="postgres://snapshot-reader",
                snapshot_ref="snapshot://repeatable-read-3",
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
        runtime_executions = runtime_model_executions(
            self.authority,
            cell,
            self.manifest,
            bundle,
            artifact_index,
        )
        journal = attempt_journal(
            self.manifest,
            artifact_set_sha256=trace_artifact_set_sha256(artifact_index),
        )
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
            runtime_executions,
            hard_checks,
        )
        trace_bundles = {cell["execution_cell_id"]: bundle}
        trace_indexes = {cell["execution_cell_id"]: artifact_index}
        executions_by_cell = {
            cell["execution_cell_id"]: runtime_executions
        }
        hard_checks_by_cell = {cell["execution_cell_id"]: hard_checks}
        suite = derive_suite_result(
            self.manifest,
            [result],
            attempt_journal=journal,
            trace_bundles=trace_bundles,
            trace_artifact_indexes=trace_indexes,
            runtime_model_executions_by_cell=executions_by_cell,
            hard_check_results=hard_checks_by_cell,
            authority=self.authority,
        )
        self.assertEqual(suite["local_execution_status"], "invalid")
        self.assertEqual(suite["coverage_admission_status"], "blocked")
        self.assertIn("run_mode_not_full", suite["coverage_blockers"])
        self.assertIn(
            "execution_manifest_invalid",
            suite["coverage_blockers"],
        )
        self.assertEqual(suite["formal_admission_status"], "blocked")
        self.assertIn("development_execution_scope", suite["formal_blockers"])

        missing = derive_suite_result(
            self.manifest,
            [],
            attempt_journal=journal,
            trace_bundles=trace_bundles,
            trace_artifact_indexes=trace_indexes,
            runtime_model_executions_by_cell=executions_by_cell,
            hard_check_results=hard_checks_by_cell,
            authority=self.authority,
        )
        self.assertEqual(missing["local_execution_status"], "invalid")
        self.assertEqual(missing["missing_cell_ids"], ["CELL-DEV-001"])

        duplicate = derive_suite_result(
            self.manifest,
            [result, result],
            attempt_journal=journal,
            trace_bundles=trace_bundles,
            trace_artifact_indexes=trace_indexes,
            runtime_model_executions_by_cell=executions_by_cell,
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
        attacked_executions = runtime_model_executions(
            self.authority,
            attacked_manifest["cells"][0],
            attacked_manifest,
            attacked_bundle,
            attacked_index,
        )
        attacked_journal = attempt_journal(
            attacked_manifest,
            artifact_set_sha256=trace_artifact_set_sha256(attacked_index),
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
            attacked_executions,
            attacked_hard_checks,
        )
        attacked_suite = derive_suite_result(
            attacked_manifest,
            [attacked_result],
            attempt_journal=attacked_journal,
            trace_bundles={"CELL-DEV-001": attacked_bundle},
            trace_artifact_indexes={"CELL-DEV-001": attacked_index},
            runtime_model_executions_by_cell={
                "CELL-DEV-001": attacked_executions
            },
            hard_check_results={"CELL-DEV-001": attacked_hard_checks},
            authority=self.authority,
        )
        self.assertEqual(attacked_suite["local_execution_status"], "invalid")
        self.assertIn(
            "execution_manifest_invalid",
            attacked_suite["coverage_blockers"],
        )

        malformed_suite = derive_suite_result(
            {},
            [],
            attempt_journal={},
            authority=self.authority,
        )
        self.assertEqual(
            malformed_suite["local_execution_status"],
            "invalid",
        )
        self.assertIn(
            "execution_manifest_invalid",
            malformed_suite["formal_blockers"],
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
        relation_group = {
            "relation_group_id": "REL-PARAPHRASE-001",
            "operator_ref": operator["operator_id"],
            "operator_sha256": canonical_sha256(operator),
            "expected_relation": operator["expected_relation"],
            "scenario_binding": None,
            "members": [
                {
                    "execution_cell_id": anchor["execution_cell_id"],
                    "member_role": "anchor",
                },
                {
                    "execution_cell_id": subject["execution_cell_id"],
                    "member_role": "subject",
                },
            ],
        }
        manifest = execution_manifest(
            self.authority,
            cells=[anchor, subject],
            relation_groups=[relation_group],
        )
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
