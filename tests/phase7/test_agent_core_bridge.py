import unittest
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from io import StringIO
from unittest.mock import patch

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.models import ConversationRunRequest
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult
from bi_agent.runtime.answer_package import (
    AuthorityFact,
    _render_authority_facts,
    build_answer_package,
    collect_visible_limitations,
    reverify_answer_package_for_delivery,
    verify_answer_package,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_digest, canonical_value
from bi_agent.runtime.claim_provenance import (
    build_context_manifest_record,
    validated_context_manifest_record,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.analysis_asset_fixtures import verified_dimension_scan_asset


class AgentCoreBridgeTest(unittest.TestCase):
    def _persisted_runtime_result(self, package, **result_fields):
        from tempfile import TemporaryDirectory

        artifact_parent = Path("artifacts/phase7/test-runtime-audit")
        artifact_parent.mkdir(parents=True, exist_ok=True)
        artifact_dir = Path(
            self.enterContext(TemporaryDirectory(dir=artifact_parent))
        )
        artifact = artifact_dir / "answer_package.json"
        artifact.write_text(json.dumps(package), encoding="utf-8")
        return {
            "run_id": package["run_id"],
            "artifact_path": str(artifact),
            "answer_package": package,
            **result_fields,
        }

    def test_context_manifest_versioned_and_legacy_digest_validation(self):
        current = build_context_manifest_record(
            run_id="run-manifest-version",
            thread_id="thread-manifest-version",
            topic_id="topic-manifest-version",
            sources=({"type": "evidence", "ref": "evidence:1", "can_support_claim": True},),
            permission_context={"role": "analyst"},
            accepted_assumptions=({"action_kind": "omit_unavailable_context"},),
        )
        self.assertEqual(current["manifest_schema_version"], "2")
        self.assertEqual(validated_context_manifest_record(current), current)

        legacy_payload = {
            key: value
            for key, value in current.items()
            if key not in {
                "accepted_assumptions", "manifest_schema_version",
                "manifest_id", "manifest_digest",
            }
        }
        legacy_digest = canonical_digest(legacy_payload)
        legacy = {
            **legacy_payload,
            "manifest_id": f"context-manifest:sha256:{legacy_digest}",
            "manifest_digest": legacy_digest,
        }
        normalized = validated_context_manifest_record(legacy)
        self.assertEqual(normalized["accepted_assumptions"], [])

        with self.assertRaisesRegex(EvidenceIntegrityError, "integrity_invalid"):
            validated_context_manifest_record({**legacy, "thread_id": "tampered"})
        with self.assertRaisesRegex(EvidenceIntegrityError, "payload_keys_invalid"):
            validated_context_manifest_record({**legacy, "unknown": True})

    def test_answer_verifier_and_authority_manifest_receive_exact_accepted_assumptions(self):
        choice = {
            "action_kind": "omit_unavailable_context",
            "affected_capabilities": ["event_evidence"],
            "degradation_decision": "continue_without_event_source",
        }
        with patch(
            "bi_agent.runtime.answer_package.verify_answer_package",
            wraps=verify_answer_package,
        ) as verifier_spy:
            package, context, _ = _verified_delivery_package(
                run_id="run-verifier-assumptions",
                accepted_assumptions=(choice,),
            )
            delivered = reverify_answer_package_for_delivery(
                package,
                evidence_resolver=context["evidence_resolver"],
                rows_loader=context["rows_loader"],
                runtime_registry=context["runtime_registry"],
                release_resolver=context["release_resolver"],
            )

        self.assertGreaterEqual(verifier_spy.call_count, 3)
        for call in verifier_spy.call_args_list:
            self.assertEqual(call.kwargs["accepted_assumptions"], (choice,))
        self.assertEqual(
            package["admin_audit"]["context_manifest"]["accepted_assumptions"],
            [choice],
        )
        self.assertEqual(delivered["context_assumptions"], [choice])

    def test_delivery_reverify_rejects_every_assumption_authority_layer_tamper(self):
        choice = {
            "action_kind": "omit_unavailable_context",
            "affected_capabilities": ["event_evidence"],
            "claim_ceiling": "observed",
        }
        package, context, _ = _verified_delivery_package(
            run_id="run-assumption-tamper",
            accepted_assumptions=(choice,),
        )
        exact = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        )
        self.assertEqual(exact["status"], "draft")
        self.assertEqual(exact["context_assumptions"], [choice])

        def changed():
            return {**choice, "claim_ceiling": "insufficient"}

        candidates = []
        context_layer = deepcopy(package)
        context_layer["context_assumptions"] = [changed()]
        candidates.append(context_layer)
        graph_layer = deepcopy(package)
        graph_layer["accepted_graph_metadata"] = {
            "accepted_assumptions": [changed()]
        }
        candidates.append(graph_layer)
        choice_layer = deepcopy(package)
        choice_layer["accepted_degradation_choice"] = changed()
        candidates.append(choice_layer)
        manifest_layer = deepcopy(package)
        manifest_layer["admin_audit"]["context_manifest"][
            "accepted_assumptions"
        ] = [changed()]
        candidates.append(manifest_layer)

        for candidate in candidates:
            with self.subTest(layer=candidates.index(candidate)):
                delivered = reverify_answer_package_for_delivery(
                    candidate,
                    evidence_resolver=context["evidence_resolver"],
                    rows_loader=context["rows_loader"],
                    runtime_registry=context["runtime_registry"],
                    release_resolver=context["release_resolver"],
                )
                self.assertEqual(delivered["status"], "failed")
                self.assertIn(
                    "accepted_assumptions_authority_mismatch",
                    {
                        item.get("code")
                        for item in delivered["admin_audit"]["verifier"]["errors"]
                    },
                )

    def test_zero_claim_degradation_has_context_only_assumption_authority(self):
        choice = {
            "choice_id": "omit-context",
            "action_kind": "omit_unavailable_context",
            "affected_capabilities": ["event_evidence"],
        }
        package = build_answer_package(
            run_id="run-zero-claim-degradation",
            draft_claims=(), evidence=(), checkpoint_events=(),
            proposed_graph=(), accepted_graph=(),
            rejected_or_degraded_mutations=(), validator_results=(),
            sql_text="", sql_hash="", artifact_audit={},
            final_explanation={
                "status": "degraded",
                "explanation": "当前证据不足以支持业务结论。",
                "owner": "业务数据负责人",
                "repair_path": "补齐背景证据后继续。",
            },
            context_manifest={
                "thread_id": "thread-zero-claim",
                "topic_id": "topic-zero-claim",
                "permission_context": {"role": "analyst"},
            },
            context_assumptions=(choice,),
            accepted_degradation_choice=choice,
            compiler_runtime_plan={
                "graph_metadata": {"accepted_assumptions": [choice]}
            },
        )
        manifest = package["admin_audit"]["context_manifest"]
        self.assertFalse(manifest["can_support_claims"])
        self.assertEqual(manifest["accepted_assumptions"], [choice])
        delivered = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=None,
            rows_loader=None,
            runtime_registry=None,
        )
        errors = {
            item.get("code")
            for item in delivered["admin_audit"]["verifier"].get("errors", ())
        }
        self.assertNotIn("accepted_assumptions_authority_mismatch", errors)

    def test_zero_claim_boundary_text_requires_typed_degradation_refs(self):
        choice = {"choice_id": "omit-context", "action_kind": "omit_unavailable_context"}
        base = {
            "final_explanation": {
                "status": "degraded",
                "explanation": "当前证据不足以支持业务结论。",
                "owner": "业务数据负责人",
                "repair_path": "补齐背景证据后继续。",
                "boundary_only": True,
                "used_contract_gap_ids": ["gap:event"],
                "used_next_action_ids": ["omit-context"],
                "structured_claim_ids": [],
            },
            "contract_gap_diagnostics": [{"gap_id": "gap:event"}],
            "accepted_degradation_choice": choice,
        }
        valid = verify_answer_package(
            draft_claims=(), evidence=(), visible_limitations=(),
            delivery_text=base, accepted_assumptions=(choice,),
        )
        self.assertNotIn(
            "free_text_without_verified_claim",
            {item.get("code") for item in valid["errors"]},
        )
        unknown = deepcopy(base)
        unknown["final_explanation"]["used_contract_gap_ids"] = ["gap:unknown"]
        mixed = deepcopy(base)
        mixed["final_explanation"]["structured_claim_ids"] = ["claim:1"]
        for candidate in (unknown, mixed):
            checked = verify_answer_package(
                draft_claims=(), evidence=(), visible_limitations=(),
                delivery_text=candidate, accepted_assumptions=(choice,),
            )
            self.assertIn(
                "free_text_without_verified_claim",
                {item.get("code") for item in checked["errors"]},
            )

    def test_agent_core_passes_and_persists_fixed_analysis_clock(self):
        captured = {}
        fixed_context = {
            "as_of": "2026-06-03T12:00:00+01:00",
            "target_date": "2026-06-02",
            "previous_day": "2026-06-01",
            "rolling_7_day_start": "2026-05-26",
            "rolling_7_day_end": "2026-06-01",
            "same_weekday_last_week": "2026-05-26",
            "pattern_history_start": "2026-01-01",
            "anomaly_history_start": "2026-05-03",
        }

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=workflow)

        core.run_message(
            thread_id="thread-fixed-clock",
            run_id="run-fixed-clock",
            user_message="昨天付费金额为什么变化？",
            analysis_context=fixed_context,
        )

        self.assertEqual(captured["analysis_context"], fixed_context)
        self.assertEqual(
            store.runs["run-fixed-clock"]["request"]["analysis_context"],
            fixed_context,
        )

    def test_agent_core_marks_analysis_runtime_requests_as_production(self):
        captured = {}

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        core = ConversationAgentCore(
            InMemoryConversationStore(),
            workflow_runner=workflow,
            analysis_runtime=object(),
        )
        core.run_message(
            thread_id="thread-production-runtime-mode",
            run_id="run-production-runtime-mode",
            user_message="昨天活跃用户如何变化？",
        )

        self.assertEqual(captured["run_mode"], "production")

    def test_agent_core_rejects_untrusted_or_invalid_analysis_clock_override(self):
        core = ConversationAgentCore(InMemoryConversationStore(), workflow_runner=fake_workflow)

        for context in (
            {"as_of": "tomorrow"},
            {"as_of": "2026-06-03T12:00:00+01:00", "source": "gateway"},
        ):
            with self.subTest(context=context), self.assertRaisesRegex(
                (PermissionError, ValueError),
                "analysis_context",
            ):
                core.run_message(
                    thread_id="thread-untrusted-clock",
                    user_message="昨天付费金额为什么变化？",
                    analysis_context=context,
                )

    def test_runtime_persistence_failure_blocks_answer_publication(self):
        class FailingStore(InMemoryConversationStore):
            def save_analysis_runtime_records(self, **kwargs):
                raise RuntimeError("postgres unavailable")

        def workflow(request):
            result = fake_workflow(request)
            return WorkflowRunResult(
                status=result.status,
                run_id=result.run_id,
                answer_package=result.answer_package,
                artifact_path=result.artifact_path,
                checkpoint_events=(
                    {"node": "persist_artifact", "status": "completed"},
                ),
                llm_calls=(_failed_llm_audit("runtime-persistence"),),
                analysis_runtime_records={"analysis_contract": {}},
            )

        store = FailingStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-persistence-failure",
            run_id="run-persistence-failure",
            user_message="昨天付费金额为什么变化？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "analysis_runtime_persistence_failed")
        self.assertEqual(
            result["llm_calls"],
            [_failed_llm_audit("runtime-persistence")],
        )
        self.assertEqual(
            store.runs["run-persistence-failure"]["checkpoint_events"],
            [{"node": "persist_artifact", "status": "completed"}],
        )
        self.assertNotIn("run-persistence-failure", store.answer_packages)
        failure_audit = next(
            item
            for item in reversed(store.audit_events)
            if item["event_type"] == "analysis_runtime_persistence_failed"
        )
        self.assertEqual(failure_audit["payload"]["reason"], "postgres unavailable")
        self.assertTrue(
            any(
                event["event_type"]
                == "workflow_failure_llm_call_recorded"
                for event in store.audit_events
            )
        )

    def test_verified_delivery_without_runtime_bundle_blocks_publication(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-missing-runtime-bundle",
        )

        result, store = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-missing-runtime-bundle",
            run_id="run-missing-runtime-bundle",
            with_runtime_records=False,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_persistence_failed",
        )
        self.assertNotIn("run-missing-runtime-bundle", store.answer_packages)
        self.assertNotIn(
            "run-missing-runtime-bundle",
            store.analysis_runtime_records,
        )
        self.assertFalse(store.analysis_assets)
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"] == "analysis_runtime_persistence_failed"
        )
        self.assertEqual(
            failure["payload"]["reason"],
            "analysis_runtime_records_missing",
        )

    def test_persisted_claim_context_manifest_is_delivery_manifest(self):
        from tests.phase7.test_analysis_runtime_persistence import _authority_bundle

        package, context, _ = _verified_delivery_package(
            run_id="run-canonical-delivery-context",
        )
        captured = {}

        def workflow(request):
            records = _authority_bundle(
                run_id=request["run_id"],
                thread_id=request["thread_id"],
                topic_id=request["topic_id"],
            )
            captured["records"] = records
            captured["request_manifest"] = request["context_manifest"]
            return WorkflowRunResult(
                status="draft",
                run_id=request["run_id"],
                answer_package=package,
                checkpoint_events=(),
                analysis_runtime_records=records,
            )

        store = InMemoryConversationStore()
        core = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            evidence_writer=context["evidence_resolver"]._runtime_writer(),
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        )
        result = core.run_message(
            thread_id="thread-canonical-delivery-context",
            run_id="run-canonical-delivery-context",
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        canonical = captured["records"]["context_manifests"][0]
        persisted_claim = captured["records"]["verified_claims"][0]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(
            captured["request_manifest"]["manifest_id"].startswith("context-")
        )
        self.assertNotEqual(
            captured["request_manifest"]["manifest_id"],
            canonical["manifest_id"],
        )
        self.assertEqual(result["context_manifest"], canonical)
        self.assertEqual(
            result["answer_package"]["verified_claims"][0][
                "context_manifest_ref"
            ],
            canonical["manifest_id"],
        )
        self.assertEqual(
            persisted_claim["context_manifest_ref"],
            canonical["manifest_id"],
        )
        self.assertEqual(
            store.context_manifests[canonical["manifest_id"]],
            canonical,
        )
        stored_claim = store.analysis_runtime_authority["verified_claim"][
            persisted_claim["claim_ref"]
        ]
        self.assertEqual(
            stored_claim["context_manifest_ref"],
            canonical["manifest_id"],
        )
        self.assertEqual(
            store.answer_packages["run-canonical-delivery-context"][
                "verified_claims"
            ][0]["context_manifest_ref"],
            canonical["manifest_id"],
        )

    def test_query_gap_answer_resumes_same_topic_and_original_analysis_lineage(self):
        captured = []

        def workflow(request):
            captured.append(dict(request))
            if len(captured) == 1:
                return WorkflowRunResult(
                    status="waiting_for_clarification",
                    run_id=request["run_id"],
                    answer_package={
                        "status": "waiting_for_clarification",
                        "accepted_graph": ["compare_periods"],
                        "analysis_contract": {
                            "analysis_contract_id": "analysis:query-gap:1",
                            "as_of": "2026-06-03T12:00:00+01:00",
                        },
                        "analysis_route": {
                            "requested_nodes": ["compare_periods"],
                            "analysis_requirements": {
                                "target_metrics": ["paid_amount"],
                                "requested_components": [],
                                "requested_dimensions": [],
                                "baselines": [],
                                "context_sources": [],
                                "claim_intents": [],
                            },
                        },
                        "original_intent": {
                            "question_family": "revenue_health_review",
                            "question_families": ["revenue_health_review"],
                            "primary_question_family": "revenue_health_review",
                            "secondary_question_families": [],
                            "target_metric": "paid_amount",
                            "baseline_candidates": [],
                            "context_sources": [],
                            "claim_intents": [],
                            "requested_dimensions": [],
                            "requested_components": [],
                            "question": "昨天付费金额为什么变化？",
                        },
                        "material_slots": {
                            "target_metrics": ["paid_amount"],
                            "requested_components": [],
                            "requested_dimensions": [],
                            "baselines": [],
                            "context_sources": [],
                            "claim_intents": [],
                        },
                        "clarification": {
                            "questions": [
                                {
                                    "question": "目标日缺数据时怎么继续？",
                                    "options": [
                                        "等待目标日刷新后继续。",
                                        "改用最近完整业务日并重新编译。",
                                        "tell the agent to do differently",
                                    ],
                                }
                            ],
                            "recommended_assumption": "改用最近完整业务日并重新编译。",
                            "choice_actions": [
                                {
                                    "choice_id": "wait",
                                    "action_kind": "wait_for_source",
                                    "business_label": "等待目标日刷新后继续。",
                                    "affected_capabilities": ["compare_periods"],
                                },
                                {
                                    "choice_id": "use_complete_day",
                                    "action_kind": "omit_unavailable_context",
                                    "business_label": "改用最近完整业务日并重新编译。",
                                    "affected_capabilities": ["event_evidence"],
                                },
                            ],
                        },
                    },
                    analysis_runtime_records={},
                )
            from bi_agent.runtime import langgraph_workflow as workflow

            workflow._bind_clarification_resume_intent(
                {
                    "question_family": "data_quality_or_evidence_review",
                    "target_metric": "paid_amount",
                    "context_sources": [],
                    "claim_intents": [],
                    "requested_dimensions": [],
                    "requested_components": [],
                },
                request,
                RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
            )
            return fake_workflow(request)

        store = InMemoryConversationStore()
        store.save_analysis_runtime_records = lambda **_: "inserted"
        recorded_outcomes = []

        def record_outcome(**kwargs):
            recorded_outcomes.append(kwargs)
            return "clarification-outcome:resolved-choice"

        resolved_authority = {
            "source_run_id": "run-query-gap-original",
            "thread_id": "thread-query-gap-resume",
            "topic_id": "topic-authority",
            "analysis_contract": {"authority": "postgres"},
            "analysis_contract_signature": "signature-authority",
            "clarification_outcome": {
                "outcome_ref": "clarification-outcome:resolved-choice"
            },
        }
        store.record_clarification_outcome = record_outcome
        store.resolve_clarification_resume_authority = (
            lambda **_: resolved_authority
        )
        core = ConversationAgentCore(store, workflow_runner=workflow)
        first = core.run_message(
            thread_id="thread-query-gap-resume",
            run_id="run-query-gap-original",
            user_message="昨天付费金额为什么变化？",
            analysis_context={"as_of": "2026-06-03T12:00:00+01:00"},
        )
        resumed = core.run_message(
            thread_id="thread-query-gap-resume",
            run_id="run-query-gap-resumed",
            user_message="按推荐继续",
            clarification={"target_window": "2026-06-01"},
        )

        self.assertEqual(first["status"], "waiting_for_clarification")
        self.assertEqual(captured[0]["context_manifest"]["accepted_assumptions"], [])
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertIsNone(resumed.get("clarification"))
        self.assertEqual(resumed["topic_id"], first["topic_id"])
        self.assertEqual(
            captured[1]["clarification_resume_context"]["resume_run_id"],
            "run-query-gap-original",
        )
        self.assertEqual(
            captured[1]["clarification_resume_context"]["accepted_graph"],
            ("compare_periods",),
        )
        self.assertEqual(
            captured[1]["clarification_choice"]["target_window"],
            "2026-06-01",
        )
        self.assertEqual(captured[1]["question"], "昨天付费金额为什么变化？")
        self.assertEqual(
            captured[1]["clarification_user_message"],
            "按推荐继续",
        )
        self.assertEqual(
            captured[1]["clarification_resume_context"]["analysis_route"]
            ["analysis_requirements"]["target_metrics"],
            ["paid_amount"],
        )
        self.assertEqual(
            captured[1]["clarification_resume_context"]["material_slots"],
            {
                "target_metrics": ["paid_amount"],
                "requested_components": [],
                "requested_dimensions": [],
                "baselines": [],
                "context_sources": [],
                "claim_intents": [],
            },
        )
        self.assertEqual(
            captured[1]["clarification_resume_context"]["selected_query_gap_action"]
            ["action_kind"],
            "omit_unavailable_context",
        )
        accepted = captured[1]["accepted_degradation_choice"]
        self.assertEqual(accepted["choice_id"], "use_complete_day")
        self.assertEqual(accepted["source_run_id"], "run-query-gap-original")
        self.assertEqual(
            captured[1]["clarification_resume_context"]["accepted_degradation_choice"],
            accepted,
        )
        self.assertEqual(
            captured[1]["context_manifest"]["accepted_assumptions"],
            [accepted],
        )
        self.assertEqual(
            captured[1]["context_manifest"]["permission_context"],
            {"role": "analyst"},
        )
        self.assertEqual(recorded_outcomes[0]["choice"], accepted)
        self.assertEqual(
            captured[1]["accepted_terminal_gap_authority"],
            resolved_authority,
        )
        self.assertEqual(
            captured[1]["clarification_outcome_ref"],
            "clarification-outcome:resolved-choice",
        )

    def test_recommended_choice_advances_with_available_work_instead_of_wait_loop(self):
        calls = []
        accepted_choice = {
            "choice_id": "omit-context",
            "action_kind": "omit_unavailable_context",
            "business_label": "跳过背景证据继续",
            "affected_capabilities": ["event_evidence"],
            "source_run_id": "run-wait-action-original",
        }
        verified_package, authority_context, _ = _verified_delivery_package(
            run_id="run-wait-action-resumed",
            accepted_assumptions=(accepted_choice,),
        )

        def workflow(request):
            calls.append(dict(request))
            if len(calls) == 1:
                return WorkflowRunResult(
                    status="waiting_for_clarification",
                    run_id=request["run_id"],
                    answer_package={
                        "status": "waiting_for_clarification",
                        "accepted_graph": ["compare_periods", "event_evidence"],
                        "analysis_contract": {"analysis_contract_id": "analysis:wait:1"},
                        "analysis_route": {"requested_nodes": ["compare_periods", "event_evidence"]},
                        "clarification": {
                            "questions": [{
                                "question": "当前来源不可用，怎么继续？",
                                "options": [
                                    "等待业务数据就绪",
                                    "跳过背景证据继续",
                                    "tell the agent to do differently",
                                ],
                            }],
                            "recommended_assumption": {"option": "等待业务数据就绪"},
                            "choice_actions": [
                                {
                                    "choice_id": "wait-source",
                                    "action_kind": "wait_for_source",
                                    "business_label": "等待业务数据就绪",
                                    "affected_capabilities": ["event_evidence"],
                                },
                                {
                                    "choice_id": "omit-context",
                                    "action_kind": "omit_unavailable_context",
                                    "business_label": "跳过背景证据继续",
                                    "affected_capabilities": ["event_evidence"],
                                },
                            ],
                        },
                    },
                    analysis_runtime_records={},
                )
            from tests.phase7.test_analysis_runtime_persistence import _authority_bundle

            records = _authority_bundle(
                run_id=request["run_id"],
                thread_id=request["thread_id"],
                topic_id=request["topic_id"],
            )
            return WorkflowRunResult(
                status="draft",
                run_id=request["run_id"],
                answer_package=verified_package,
                analysis_runtime_records=records,
            )

        store = InMemoryConversationStore()
        store.save_analysis_runtime_records = lambda **_: "inserted"
        store.record_clarification_outcome = (
            lambda **_: "clarification-outcome:wait-action"
        )
        store.resolve_clarification_resume_authority = lambda **kwargs: {
            "source_run_id": kwargs["source_run_id"],
            "thread_id": kwargs["thread_id"],
            "topic_id": kwargs["topic_id"],
            "analysis_contract": {"authority": "postgres"},
            "analysis_contract_signature": "signature-authority",
            "clarification_outcome": {"outcome_ref": kwargs["outcome_ref"]},
        }
        core = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            evidence_resolver=authority_context["evidence_resolver"],
            rows_loader=authority_context["rows_loader"],
            evidence_writer=authority_context["evidence_resolver"]._runtime_writer(),
            runtime_registry=authority_context["runtime_registry"],
            release_resolver=authority_context["release_resolver"],
        )
        first = core.run_message(
            thread_id="thread-wait-action",
            run_id="run-wait-action-original",
            user_message="活动是否影响昨天？",
        )
        original_to_dict = ConversationRunRequest.to_dict

        def persisted_projection(run_request):
            payload = original_to_dict(run_request)
            payload["clarification_resume_context"].pop(
                "accepted_degradation_choice", None
            )
            return payload

        with patch.object(
            ConversationRunRequest,
            "to_dict",
            persisted_projection,
        ):
            resumed = core.run_message(
                thread_id="thread-wait-action",
                run_id="run-wait-action-resumed",
                user_message="按推荐继续",
                clarification={"answer_text": "按推荐继续"},
            )

        self.assertEqual(first["status"], "waiting_for_clarification")
        self.assertEqual(
            first["clarification"]["recommended_choice_id"],
            "omit-context",
        )
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertEqual(resumed["topic_id"], first["topic_id"])
        self.assertNotIn("clarification", resumed)
        self.assertEqual(
            resumed["answer_package"]["verified_claims"][0]["claim_strength"],
            "observed",
        )
        self.assertEqual(
            resumed["answer_package"]["context_assumptions"],
            [accepted_choice],
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[1]["accepted_degradation_choice"]["action_kind"],
            "omit_unavailable_context",
        )

    def test_no_ready_capability_recommends_boundary_terminal_and_resumes_once(self):
        calls = []

        def workflow(request):
            calls.append(dict(request))
            if len(calls) == 1:
                return WorkflowRunResult(
                    status="waiting_for_clarification",
                    run_id=request["run_id"],
                    answer_package={
                        "status": "waiting_for_clarification",
                        "accepted_graph": ["event_evidence"],
                        "analysis_contract": {"analysis_contract_id": "analysis:no-ready:1"},
                        "analysis_route": {"requested_nodes": ["event_evidence"]},
                        "clarification": {
                            "questions": [{
                                "question": "当前没有可执行证据路径，怎么继续？",
                                "options": [
                                    "等待业务数据就绪",
                                    "完成证据边界说明",
                                    "tell the agent to do differently",
                                ],
                            }],
                            "recommended_assumption": {"option": "等待业务数据就绪"},
                            "choice_actions": [
                                {
                                    "choice_id": "wait-source",
                                    "action_kind": "wait_for_source",
                                    "business_label": "等待业务数据就绪",
                                    "affected_capabilities": ["event_evidence"],
                                },
                                {
                                    "choice_id": "boundary-only",
                                    "action_kind": "continue_with_boundary_only",
                                    "business_label": "完成证据边界说明",
                                    "affected_capabilities": ["event_evidence"],
                                },
                            ],
                        },
                    },
                    analysis_runtime_records={},
                )
            return fake_workflow(request)

        store = InMemoryConversationStore()
        store.save_analysis_runtime_records = lambda **_: "inserted"
        store.record_clarification_outcome = (
            lambda **_: "clarification-outcome:no-ready"
        )
        store.resolve_clarification_resume_authority = lambda **kwargs: {
            "source_run_id": kwargs["source_run_id"],
            "thread_id": kwargs["thread_id"],
            "topic_id": kwargs["topic_id"],
            "analysis_contract": {"authority": "postgres"},
            "analysis_contract_signature": "signature-authority",
            "clarification_outcome": {"outcome_ref": kwargs["outcome_ref"]},
        }
        core = ConversationAgentCore(store, workflow_runner=workflow)
        first = core.run_message(
            thread_id="thread-no-ready-boundary",
            run_id="run-no-ready-original",
            user_message="现有证据能否支持这个判断？",
        )
        resumed = core.run_message(
            thread_id="thread-no-ready-boundary",
            run_id="run-no-ready-resumed",
            user_message="按推荐继续",
            clarification={"answer_text": "按推荐继续"},
        )

        self.assertEqual(first["status"], "waiting_for_clarification")
        self.assertEqual(first["clarification"]["recommended_choice_id"], "boundary-only")
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[1]["accepted_degradation_choice"]["action_kind"],
            "continue_with_boundary_only",
        )

    def test_query_gap_waiting_persists_zero_claim_runtime_bundle_before_return(self):
        class CapturingStore(InMemoryConversationStore):
            def save_analysis_runtime_records(self, **kwargs):
                self.saved_runtime_bundle = kwargs
                return "inserted"

        records = {
            "analysis_contract": {"analysis_contract_id": "analysis:waiting:1"},
            "query_contracts": (),
            "query_execution_records": (),
            "rows_records": (),
            "snapshot_records": (),
            "completeness_records": (),
            "capability_binding_records": (),
            "evidence_manifests": (),
            "context_manifests": (),
            "trusted_provenance_records": (),
            "verified_claims": (),
            "claim_links": (),
            "repair_attempts": (),
        }

        def workflow(request):
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["event_evidence"],
                    "analysis_contract": records["analysis_contract"],
                    "analysis_route": {"requested_nodes": ["event_evidence"]},
                    "clarification": {
                        "questions": [{
                            "question": "当前来源不可用，怎么继续？",
                            "options": [
                                "等待业务数据就绪",
                                "tell the agent to do differently",
                            ],
                        }],
                        "recommended_assumption": {"option": "等待业务数据就绪"},
                    },
                },
                analysis_runtime_records=records,
            )

        store = CapturingStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-waiting-runtime-persistence",
            run_id="run-waiting-runtime-persistence",
            user_message="活动是否影响昨天？",
        )

        self.assertEqual(result["status"], "waiting_for_clarification")
        self.assertEqual(
            store.saved_runtime_bundle["analysis_contract"]["analysis_contract_id"],
            "analysis:waiting:1",
        )
        self.assertEqual(store.saved_runtime_bundle["run_id"], result["run_id"])

    def test_typed_query_gap_without_runtime_bundle_fails_closed(self):
        def workflow(request):
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["event_evidence"],
                    "analysis_contract": {
                        "analysis_contract_id": "analysis:missing:1"
                    },
                    "analysis_route": {"requested_nodes": ["event_evidence"]},
                    "clarification": {
                        "questions": [{
                            "question": "当前来源不可用，怎么继续？",
                            "options": [
                                "等待业务数据就绪",
                                "tell the agent to do differently",
                            ],
                        }],
                        "recommended_assumption": {"option": "等待业务数据就绪"},
                    },
                },
                checkpoint_events=(
                    {"node": "compile_analysis_contract", "status": "completed"},
                ),
                llm_calls=(_failed_llm_audit("waiting-persistence"),),
                analysis_runtime_records=None,
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            analysis_runtime=object(),
        ).run_message(
            thread_id="thread-missing-waiting-runtime-bundle",
            run_id="run-missing-waiting-runtime-bundle",
            user_message="活动是否影响昨天？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_persistence_failed",
        )
        self.assertEqual(
            result["llm_calls"],
            [_failed_llm_audit("waiting-persistence")],
        )
        self.assertEqual(
            store.runs[result["run_id"]]["checkpoint_events"],
            [{"node": "compile_analysis_contract", "status": "completed"}],
        )
        self.assertTrue(
            any(
                event["event_type"]
                == "workflow_failure_llm_call_recorded"
                for event in store.audit_events
            )
        )
        self.assertNotIn(result["run_id"], store.analysis_runtime_records)
        self.assertEqual(
            store.get_thread(
                "thread-missing-waiting-runtime-bundle"
            ).pending_clarification_id,
            "",
        )

    def test_query_gap_user_redirect_opens_free_input_without_rerunning_workflow(self):
        calls = []

        def workflow(request):
            calls.append(dict(request))
            if len(calls) > 1:
                raise AssertionError("user redirect must not rerun analysis")
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["event_evidence"],
                    "analysis_route": {"requested_nodes": ["event_evidence"]},
                    "clarification": {
                        "questions": [{
                            "question": "当前来源不可用，怎么继续？",
                            "options": [
                                "等待业务数据就绪",
                                "tell the agent to do differently",
                            ],
                        }],
                        "recommended_assumption": {"option": "等待业务数据就绪"},
                        "choice_actions": [
                            {
                                "choice_id": "wait-source",
                                "action_kind": "wait_for_source",
                                "business_label": "等待业务数据就绪",
                                "affected_capabilities": ["event_evidence"],
                            },
                            {
                                "choice_id": "user_redirect",
                                "action_kind": "user_redirect",
                                "business_label": "tell the agent to do differently",
                                "affected_capabilities": [],
                            },
                        ],
                    },
                },
            )

        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=workflow)
        core.run_message(
            thread_id="thread-user-redirect",
            run_id="run-user-redirect-original",
            user_message="活动是否影响昨天？",
        )
        redirected = core.run_message(
            thread_id="thread-user-redirect",
            run_id="run-user-redirect-selected",
            user_message="tell the agent to do differently",
            clarification={"answer_text": "tell the agent to do differently"},
        )

        self.assertEqual(redirected["status"], "waiting_for_clarification")
        self.assertTrue(redirected["user_redirect"])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(store.get_open_clarification("thread-user-redirect"))

    def test_general_clarification_resumes_original_intent_without_query_gap_action(self):
        captured = []

        def workflow(request):
            captured.append(dict(request))
            if len(captured) == 1:
                return WorkflowRunResult(
                    status="waiting_for_clarification",
                    run_id=request["run_id"],
                    answer_package={
                        "status": "waiting_for_clarification",
                        "accepted_graph": [],
                        "analysis_contract": {},
                        "analysis_route": {
                            "requested_nodes": [],
                            "analysis_requirements": {
                                "target_metrics": ["active_users"],
                                "baselines": ["previous_day"],
                            },
                        },
                        "original_intent": {
                            "target_metric": "active_users",
                            "question_family": "market_health_comparison",
                        },
                        "material_slots": {
                            "target_metrics": ["active_users"],
                            "baselines": ["previous_day"],
                        },
                        "clarification": {
                            "questions": [{
                                "question": "按哪个范围继续？",
                                "options": [
                                    "保留全样本范围继续。",
                                    "调整业务范围后继续。",
                                    "tell the agent to do differently",
                                ],
                            }],
                            "recommended_assumption": {
                                "option": "保留全样本范围继续。"
                            },
                        },
                    },
                )
            return fake_workflow(request)

        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=workflow)
        first = core.run_message(
            thread_id="thread-general-clarification",
            run_id="run-general-original",
            user_message="比较昨天活跃用户并说明范围。",
            analysis_context={"as_of": "2026-06-03T12:00:00+01:00"},
        )
        resumed = core.run_message(
            thread_id="thread-general-clarification",
            run_id="run-general-resumed",
            user_message="保留全样本范围继续。",
            clarification={"answer_text": "保留全样本范围继续。"},
        )

        self.assertEqual(first["status"], "waiting_for_clarification")
        self.assertEqual(resumed["topic_id"], first["topic_id"])
        resume = captured[1]["clarification_resume_context"]
        self.assertEqual(resume["resume_run_id"], "run-general-original")
        self.assertEqual(resume["original_intent"]["target_metric"], "active_users")
        self.assertEqual(resume["material_slots"]["baselines"], ["previous_day"])
        self.assertEqual(resume["selected_query_gap_action"], {})
        self.assertEqual(captured[1]["question"], "比较昨天活跃用户并说明范围。")

    def test_resigned_claim_and_evidence_numbers_cannot_extend_persisted_facts(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-resigned-facts",
        )
        claim = package["sections"][0]["payload"]["claims"][0]
        evidence = package["sections"][1]["payload"]["evidence"][0]
        claim["numbers"] = {"paid_amount": 999999.0}
        claim["text"] = "paid_amount 为 999999。"
        evidence["typed_payload"] = {"paid_amount": 999999.0}
        _resign_reported_verifier(package, context)

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-resigned-facts",
            run_id="run-agent-core-resigned-facts",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "failed")
        self.assertEqual(delivered["final_answer"], "")

    def test_target_and_baseline_values_cannot_cross_window_roles(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-role-swap",
            paid_amount=999999.0,
            baseline_amount=10.0,
            claim_text="目标期 paid_amount 为 10，基线为 999999。",
            claim_numbers={
                "target_paid_amount": 10.0,
                "baseline_paid_amount": 999999.0,
            },
        )

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-role-swap",
            run_id="run-agent-core-role-swap",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "failed")

    def test_factual_language_is_canonically_rendered_without_caller_commentary(self):
        for case_id, text, forbidden in (
            ("less_than", "paid_amount 少于 10。", "少于"),
            ("over", "paid_amount 超过 999。", "999"),
            ("halved", "paid_amount 减半到 10。", "减半"),
        ):
            with self.subTest(case_id=case_id):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-agent-core-render-{case_id}",
                    claim_text=text,
                )
                delivered = _run_verified_package_through_core(
                    package,
                    context,
                    thread_id=f"thread-agent-core-render-{case_id}",
                    run_id=f"run-agent-core-render-{case_id}",
                )[0]["answer_package"]
                self.assertEqual(delivered["status"], "draft")
                self.assertNotIn(forbidden, delivered["final_answer"])
                self.assertIn("paid_amount", delivered["final_answer"])

        package, context, commentary = _verified_delivery_package(
            run_id="run-agent-core-commentary",
            claim_text="建议提升体验并持续观察。",
        )
        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-commentary",
            run_id="run-agent-core-commentary",
        )[0]["answer_package"]
        self.assertNotIn(commentary, delivered["final_answer"])
        self.assertNotIn(commentary, json.dumps(delivered, ensure_ascii=False))
        self.assertEqual(
            delivered["final_answer"],
            delivered["sections"][0]["payload"]["claims"][0]["text"],
        )

    def test_raw_scalar_display_cannot_be_changed_by_caller_percent_text(self):
        raw_decimal, raw_decimal_context, _ = _verified_delivery_package(
            run_id="run-agent-core-raw-decimal",
            paid_amount=0.123,
            claim_text="paid_amount 为 12.3%。",
        )
        raw_decimal_delivery = _run_verified_package_through_core(
            raw_decimal,
            raw_decimal_context,
            thread_id="thread-agent-core-raw-decimal",
            run_id="run-agent-core-raw-decimal",
        )[0]["answer_package"]
        self.assertIn("=0.123", raw_decimal_delivery["final_answer"])
        self.assertNotIn("%", raw_decimal_delivery["final_answer"])

        raw_ten, raw_ten_context, _ = _verified_delivery_package(
            run_id="run-agent-core-raw-ten",
            paid_amount=10,
            claim_text="paid_amount 为 1000%。",
        )
        raw_ten_delivery = _run_verified_package_through_core(
            raw_ten,
            raw_ten_context,
            thread_id="thread-agent-core-raw-ten",
            run_id="run-agent-core-raw-ten",
        )[0]["answer_package"]
        self.assertIn("=10", raw_ten_delivery["final_answer"])
        self.assertNotIn("%", raw_ten_delivery["final_answer"])

    def test_ratio_display_policy_renders_canonical_percent(self):
        fact = AuthorityFact.create(
            query_contract_ref="query:ratio",
            result_ref="result:ratio",
            metric_id="payment_success_rate",
            value=Decimal("0.123"),
            window_id="target_day",
            window_role="target",
            observation_key="2026-06-02",
            dimensions=(),
            grain=("window_id", "observation_key"),
            value_semantics="scalar_ratio",
            display_format="percent",
        )

        rendered = _render_authority_facts(
            (
                {
                    "kind": "fact",
                    "metric_id": fact.metric_id,
                    "value": fact.value,
                    "window_id": fact.window_id,
                    "window_role": fact.window_role,
                    "observation_key": fact.observation_key,
                    "dimensions": fact.dimensions,
                    "value_semantics": fact.value_semantics,
                    "display_format": fact.display_format,
                },
            )
        )

        self.assertEqual(
            rendered,
            ("目标期（target_day，2026-06-02）payment_success_rate=12.3%。",),
        )

    def test_factless_caller_prose_never_enters_client_projection(self):
        for case_id, text in (
            ("recommend_percent", "建议100%"),
            ("double", "翻倍"),
            ("large_decline", "大幅下降"),
            ("main_driver", "主要驱动因素"),
        ):
            with self.subTest(case_id=case_id):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-agent-core-factless-{case_id}",
                    claim_text=text,
                    claim_numbers={},
                )
                delivered = _run_verified_package_through_core(
                    package,
                    context,
                    thread_id=f"thread-agent-core-factless-{case_id}",
                    run_id=f"run-agent-core-factless-{case_id}",
                )[0]["answer_package"]
                summary = delivered["sections"][0]["payload"]
                self.assertEqual(delivered["final_answer"], "")
                self.assertEqual(summary["claims"], [])
                self.assertEqual(summary["claim_groups"], [])
                self.assertEqual(summary["visualization_plan"]["blocks"], [])
                self.assertNotIn(text, json.dumps(delivered, ensure_ascii=False))

    def test_dimension_fact_requires_unique_authoritative_row_key(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-dimension-ambiguous",
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": "B",
                },
            ),
        )
        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-dimension-ambiguous",
            run_id="run-agent-core-dimension-ambiguous",
        )[0]["answer_package"]
        self.assertEqual(delivered["status"], "failed")

    def test_complete_fact_selector_uniquely_binds_window_observation_and_dimension(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-selector-complete",
            claim_selector_mode="target",
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": "B",
                },
            ),
        )
        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-selector-complete",
            run_id="run-agent-core-selector-complete",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "draft")
        summary = delivered["sections"][0]["payload"]
        self.assertEqual(len(summary["claims"]), 1)
        self.assertEqual(summary["claims"][0]["dimensions"], {"channel": "A"})
        self.assertEqual(
            summary["claims"][0]["text"],
            summary["claim_groups"][0]["text"],
        )
        self.assertEqual(
            summary["claims"][0]["text"],
            summary["visualization_plan"]["blocks"][0]["claim_text"],
        )
        self.assertEqual(delivered["final_answer"], summary["claims"][0]["text"])

    def test_wrong_explicit_fact_selector_is_rejected(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-selector-wrong",
        )
        claim = package["sections"][0]["payload"]["claims"][0]
        selector = _fact_selector(
            package,
            context,
            window_role="target",
            window_id="target_day",
            observation_key="2026-06-02",
            dimensions={"channel": "A"},
        )
        selector["observation_key"] = "2026-06-01"
        claim["fact_selectors"] = {"paid_amount": selector}
        _resign_reported_verifier(package, context)

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-selector-wrong",
            run_id="run-agent-core-selector-wrong",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "failed")
        self.assertEqual(delivered["final_answer"], "")

    def test_fully_selected_delta_binds_unique_target_and_baseline_pair(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-selector-delta",
            paid_amount=10.0,
            baseline_amount=7.0,
            claim_text="paid_amount delta is 3.",
            claim_numbers={"delta": 3.0},
            claim_selector_mode="delta_pair",
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": "B",
                },
                {
                    "window_id": "baseline_day",
                    "window_role": "baseline",
                    "observation_key": "2026-06-01",
                    "paid_amount": 16.0,
                    "amount": 16.0,
                    "channel": "B",
                },
            ),
        )
        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-selector-delta",
            run_id="run-agent-core-selector-delta",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "draft")
        self.assertIn("增加3", delivered["final_answer"])

    def test_delta_value_cannot_select_one_of_multiple_authority_pairs(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-delta-value-selector",
            paid_amount=10.0,
            baseline_amount=7.0,
            claim_text="paid_amount delta is 3.",
            claim_numbers={"delta": 3.0},
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": "B",
                },
                {
                    "window_id": "baseline_day",
                    "window_role": "baseline",
                    "observation_key": "2026-06-01",
                    "paid_amount": 16.0,
                    "amount": 16.0,
                    "channel": "B",
                },
            ),
        )

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-delta-value-selector",
            run_id="run-agent-core-delta-value-selector",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "failed")
        self.assertEqual(delivered["final_answer"], "")

    def test_selected_delta_pair_checks_arithmetic_only_after_selection(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-delta-wrong-arithmetic",
            paid_amount=10.0,
            baseline_amount=7.0,
            claim_text="paid_amount delta is 4.",
            claim_numbers={"delta": 4.0},
            claim_selector_mode="delta_pair",
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": "B",
                },
                {
                    "window_id": "baseline_day",
                    "window_role": "baseline",
                    "observation_key": "2026-06-01",
                    "paid_amount": 16.0,
                    "amount": 16.0,
                    "channel": "B",
                },
            ),
        )
        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-delta-wrong-arithmetic",
            run_id="run-agent-core-delta-wrong-arithmetic",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "failed")

    def test_equal_delta_multiple_pairs_remain_ambiguous_without_selector(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-delta-equal-ambiguous",
            paid_amount=10.0,
            baseline_amount=7.0,
            claim_text="paid_amount delta is 3.",
            claim_numbers={"delta": 3.0},
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": "B",
                },
                {
                    "window_id": "baseline_day",
                    "window_role": "baseline",
                    "observation_key": "2026-06-01",
                    "paid_amount": 17.0,
                    "amount": 17.0,
                    "channel": "B",
                },
            ),
        )

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-delta-equal-ambiguous",
            run_id="run-agent-core-delta-equal-ambiguous",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "failed")

    def test_dimension_scalar_types_are_canonical_and_distinct(self):
        cases = (
            ("zero", 0, "integer", 0),
            ("false", False, "boolean", False),
            ("null", None, "null", "Unknown"),
            ("empty", "", "string", ""),
        )
        for case_id, value, value_type, projected_value in cases:
            with self.subTest(case_id=case_id):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-agent-core-dimension-scalar-{case_id}",
                    channel=value,
                    claim_dimensions={"channel": value},
                )
                delivered = _run_verified_package_through_core(
                    package,
                    context,
                    thread_id=f"thread-agent-core-dimension-scalar-{case_id}",
                    run_id=f"run-agent-core-dimension-scalar-{case_id}",
                )[0]["answer_package"]

                self.assertEqual(delivered["status"], "draft")
                projected = delivered["sections"][0]["payload"]["claims"][0]
                self.assertTrue(projected["fact_refs"])
                self.assertEqual(projected["dimensions"]["channel"], projected_value)
                selector = projected["fact_selectors"]["paid_amount"]["dimensions"][
                    "channel"
                ]
                self.assertEqual(selector["value_type"], value_type)
                self.assertEqual(selector["value"], projected_value)

    def test_dimension_scalar_types_coexist_as_distinct_authority_keys(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-dimension-scalars-coexist",
            channel=0,
            paid_amount=10.0,
            claim_dimensions={"channel": 0},
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": False,
                },
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 30.0,
                    "amount": 30.0,
                    "channel": None,
                },
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 40.0,
                    "amount": 40.0,
                    "channel": "",
                },
            ),
        )

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-dimension-scalars-coexist",
            run_id="run-agent-core-dimension-scalars-coexist",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "draft")
        projected = delivered["sections"][0]["payload"]["claims"][0]
        self.assertEqual(projected["dimensions"]["channel"], 0)

    def test_number_dimension_selector_preserves_high_precision_and_replays(self):
        value = Decimal("12345678901234567890.123456789012345678901234567890")
        fixed = "12345678901234567890.12345678901234567890123456789"
        canonical = f"{fixed[0]}.{fixed[1:].replace('.', '')}E+19"
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-dimension-high-precision",
            channel=value,
        )

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-dimension-high-precision",
            run_id="run-agent-core-dimension-high-precision",
        )[0]["answer_package"]
        projected = delivered["sections"][0]["payload"]["claims"][0]
        fact_selector = projected["fact_selectors"]["paid_amount"]

        self.assertEqual(projected["dimensions"]["channel"], canonical)
        self.assertEqual(
            fact_selector["dimensions"]["channel"],
            {
                "value_type": "number",
                "canonical_value": canonical,
                "display_value": canonical,
            },
        )
        claim = package["sections"][0]["payload"]["claims"][0]
        claim.pop("dimensions", None)
        claim["fact_selectors"] = {"paid_amount": fact_selector}
        _resign_reported_verifier(package, context)

        replayed = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-dimension-high-precision-replay",
            run_id="run-agent-core-dimension-high-precision",
        )[0]["answer_package"]

        self.assertEqual(replayed["status"], "draft")
        self.assertEqual(
            replayed["sections"][0]["payload"]["claims"][0]["fact_selectors"],
            projected["fact_selectors"],
        )

    def test_number_dimension_selector_preserves_extreme_decimal_magnitudes(self):
        cases = (
            ("large", Decimal("1E+100"), "1E+100"),
            ("small", Decimal("1E-100"), "1E-100"),
        )
        for case_id, value, canonical in cases:
            with self.subTest(case_id=case_id):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-agent-core-dimension-{case_id}-decimal",
                    channel=value,
                )
                delivered = _run_verified_package_through_core(
                    package,
                    context,
                    thread_id=f"thread-agent-core-dimension-{case_id}-decimal",
                    run_id=f"run-agent-core-dimension-{case_id}-decimal",
                )[0]["answer_package"]
                selector = delivered["sections"][0]["payload"]["claims"][0][
                    "fact_selectors"
                ]["paid_amount"]["dimensions"]["channel"]

                self.assertEqual(selector.get("canonical_value"), canonical)
                self.assertEqual(selector.get("display_value"), canonical)
                self.assertNotIsInstance(selector.get("canonical_value"), float)

    def test_scientific_number_dimension_selector_uses_equivalent_canonical_value(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-dimension-scientific",
            channel=Decimal("1.2300E+3"),
        )

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-dimension-scientific",
            run_id="run-agent-core-dimension-scientific",
        )[0]["answer_package"]
        selector = delivered["sections"][0]["payload"]["claims"][0][
            "fact_selectors"
        ]["paid_amount"]["dimensions"]["channel"]

        self.assertEqual(selector.get("canonical_value"), "1.23E+3")
        self.assertEqual(selector.get("display_value"), "1.23E+3")
        claim = package["sections"][0]["payload"]["claims"][0]
        legacy_selector = dict(
            delivered["sections"][0]["payload"]["claims"][0][
                "fact_selectors"
            ]["paid_amount"]
        )
        legacy_selector["dimensions"] = {
            "channel": {"value_type": "number", "value": "1230.0000"}
        }
        claim["fact_selectors"] = {"paid_amount": legacy_selector}
        _resign_reported_verifier(package, context)

        equivalent = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-dimension-scientific-equivalent",
            run_id="run-agent-core-dimension-scientific",
        )[0]["answer_package"]

        self.assertEqual(equivalent["status"], "draft")

    def test_dimension_number_exponent_resource_boundary_fails_closed(self):
        for case_id, value in (
            ("huge_positive_exponent", Decimal("1E+1000000")),
            ("huge_negative_exponent", Decimal("1E-1000000")),
        ):
            with self.subTest(case_id=case_id):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-agent-core-dimension-{case_id}",
                    channel=value,
                )
                delivered = _run_verified_package_through_core(
                    package,
                    context,
                    thread_id=f"thread-agent-core-dimension-{case_id}",
                    run_id=f"run-agent-core-dimension-{case_id}",
                )[0]["answer_package"]

                self.assertEqual(delivered["status"], "failed")
                self.assertEqual(delivered["final_answer"], "")

    def test_non_finite_dimension_numbers_are_rejected_by_authority_rows(self):
        for case_id, value in (
            ("nan", Decimal("NaN")),
            ("positive_infinity", Decimal("Infinity")),
            ("negative_infinity", Decimal("-Infinity")),
        ):
            with self.subTest(case_id=case_id), self.assertRaisesRegex(
                EvidenceIntegrityError,
                "canonical_number_not_finite",
            ):
                _verified_delivery_package(
                    run_id=f"run-agent-core-dimension-{case_id}",
                    channel=value,
                    claim_dimensions={"channel": value},
                )

    def test_empty_string_selector_cannot_match_numeric_zero_dimension(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-dimension-zero-empty-mismatch",
            channel=0,
            claim_dimensions={"channel": ""},
        )

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-dimension-zero-empty-mismatch",
            run_id="run-agent-core-dimension-zero-empty-mismatch",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "failed")

    def test_dimension_scalar_selector_types_do_not_cross_match(self):
        cases = (
            ("zero_vs_string_zero", 0, "0"),
            ("false_vs_zero", False, 0),
            ("null_vs_empty", None, ""),
        )
        for case_id, actual, selector in cases:
            with self.subTest(case_id=case_id):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-agent-core-dimension-mismatch-{case_id}",
                    channel=actual,
                    claim_dimensions={"channel": selector},
                )
                delivered = _run_verified_package_through_core(
                    package,
                    context,
                    thread_id=f"thread-agent-core-dimension-mismatch-{case_id}",
                    run_id=f"run-agent-core-dimension-mismatch-{case_id}",
                )[0]["answer_package"]

                self.assertEqual(delivered["status"], "failed")

    def test_dimension_selector_resolves_unique_authority_row(self):
        selected, selected_context, _ = _verified_delivery_package(
            run_id="run-agent-core-dimension-selected",
            extra_rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 20.0,
                    "amount": 20.0,
                    "channel": "B",
                },
            ),
            claim_dimensions={"channel": "A"},
        )
        selected_delivery = _run_verified_package_through_core(
            selected,
            selected_context,
            thread_id="thread-agent-core-dimension-selected",
            run_id="run-agent-core-dimension-selected",
        )[0]["answer_package"]
        self.assertEqual(selected_delivery["status"], "draft")
        self.assertIn("channel=A", selected_delivery["final_answer"])

    def test_verified_numbers_do_not_authorize_unbound_text_facts(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-factual-injection",
        )
        claim = package["sections"][0]["payload"]["claims"][0]
        claim["text"] = "paid_amount=999999，ROI=123。"
        package["final_answer"] = claim["text"]

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-factual-injection",
            run_id="run-agent-core-factual-injection",
        )[0]["answer_package"]

        self.assertEqual(delivered["status"], "draft")
        self.assertNotIn("999999", delivered["final_answer"])
        self.assertNotIn("ROI", delivered["final_answer"])
        self.assertNotIn("123", delivered["final_answer"])
        self.assertIn("=10", delivered["final_answer"])

    def test_target_context_and_evidence_strength_come_from_authority(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-authority-projection",
        )
        claim = package["sections"][0]["payload"]["claims"][0]
        claim.update(
            {
                "target_metric": "ROI",
                "baseline": {"label": "forged baseline"},
                "target": {"label": "forged target"},
            }
        )
        package["sections"][1]["payload"]["evidence"][0]["strength"] = "strong"

        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-authority-projection",
            run_id="run-agent-core-authority-projection",
        )[0]["answer_package"]
        projected = delivered["sections"][0]["payload"]["claims"][0]
        projected_evidence = delivered["sections"][1]["payload"]["evidence"][0]

        self.assertEqual(projected["target_metric"], "paid_amount")
        self.assertEqual(projected["target"]["window_id"], "target_day")
        self.assertNotIn("baseline", projected)
        self.assertEqual(projected_evidence["strength"], "observed")
        self.assertNotIn("forged", str(delivered))

    def test_authority_bound_fact_formats_ignore_caller_wording_and_scale(self):
        cases = (
            (
                "negative_thousands_date",
                -1234.5,
                "2026-06-02 的 paid_amount 为 -1,234.50。",
            ),
            (
                "percentage_date",
                0.123,
                "2026-06-02 的 paid_amount 为 12.3%。",
            ),
            (
                "pure_wording",
                10.0,
                "渠道表现值得关注，建议持续观察。",
            ),
        )
        for case_id, paid_amount, claim_text in cases:
            with self.subTest(case_id=case_id):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-agent-core-{case_id}",
                    paid_amount=paid_amount,
                    claim_text=claim_text,
                )
                delivered = _run_verified_package_through_core(
                    package,
                    context,
                    thread_id=f"thread-agent-core-{case_id}",
                    run_id=f"run-agent-core-{case_id}",
                )[0]["answer_package"]

                self.assertEqual(delivered["status"], "draft")
                if case_id == "negative_thousands_date":
                    self.assertIn("2026-06-02", delivered["final_answer"])
                    self.assertIn("-1,234.5", delivered["final_answer"])
                elif case_id == "percentage_date":
                    self.assertIn("2026-06-02", delivered["final_answer"])
                    self.assertIn("=0.123", delivered["final_answer"])
                    self.assertNotIn("%", delivered["final_answer"])
                else:
                    self.assertNotIn(claim_text, delivered["final_answer"])
                    self.assertIn("paid_amount", delivered["final_answer"])

    def test_comparison_direction_must_match_authoritative_windows(self):
        package, context, claim_text = _verified_delivery_package(
            run_id="run-agent-core-direction",
            paid_amount=20.0,
            baseline_amount=10.0,
            claim_text="目标期 paid_amount 为 20，高于基线的 10。",
            claim_numbers={
                "target_paid_amount": 20.0,
                "baseline_paid_amount": 10.0,
                "delta": 10.0,
            },
        )
        delivered = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-direction",
            run_id="run-agent-core-direction",
        )[0]["answer_package"]

        projected = delivered["sections"][0]["payload"]["claims"][0]
        self.assertIn("paid_amount", delivered["final_answer"])
        self.assertNotEqual(delivered["final_answer"], claim_text)
        self.assertEqual(projected["comparison_direction"], "positive")
        self.assertEqual(projected["baseline"]["window_id"], "baseline_day")

        package["sections"][0]["payload"]["claims"][0]["text"] = (
            "目标期 paid_amount 为 20，低于基线的 10。"
        )
        rejected = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-wrong-direction",
            run_id="run-agent-core-wrong-direction",
        )[0]["answer_package"]
        self.assertEqual(rejected["status"], "draft")
        self.assertNotIn("低于", rejected["final_answer"])
        self.assertIn("增加10", rejected["final_answer"])

    def test_valid_claim_cannot_carry_unbound_client_content(self):
        package, context, claim_text = _verified_delivery_package(
            run_id="run-agent-core-closed-projection",
        )
        package["final_answer"] = "INJECTED FINAL"
        package["sections"][0]["payload"]["answer_text"] = "INJECTED SUMMARY"
        package["sections"][0]["payload"]["claim_groups"].append(
            {"text": "INJECTED GROUP"}
        )
        package["sections"][0]["payload"]["visualization_plan"]["blocks"].append(
            {"claim_text": "INJECTED VISUAL"}
        )
        package["llm_calls"] = [{"output": "INJECTED LLM"}]
        package["admin_audit"]["client_note"] = "INJECTED ADMIN"

        result, _ = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-closed-projection",
            run_id="run-agent-core-closed-projection",
        )

        delivered = result["answer_package"]
        summary = delivered["sections"][0]["payload"]
        self.assertIn("paid_amount", delivered["final_answer"])
        self.assertEqual(summary["answer_text"], delivered["final_answer"])
        self.assertEqual(len(summary["claim_groups"]), 1)
        self.assertEqual(len(summary["visualization_plan"]["blocks"]), 1)
        self.assertEqual(delivered["llm_calls"], [])
        self.assertNotIn("INJECTED", str(delivered))

    def test_warnings_only_verified_claim_remains_deliverable(self):
        package, context, claim_text = _verified_delivery_package(
            run_id="run-agent-core-warning-delivery",
            causal_wording=True,
        )

        result, store = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-warning-delivery",
            run_id="run-agent-core-warning-delivery",
        )
        delivered = result["answer_package"]

        self.assertEqual(
            delivered["admin_audit"]["verifier"]["status"],
            "passed_with_warnings",
        )
        self.assertIn("paid_amount", delivered["final_answer"])
        self.assertNotEqual(delivered["final_answer"], claim_text)
        self.assertNotIn("message", str(delivered["admin_audit"]))
        internal = next(
            event["payload"]
            for event in store.audit_events
            if event["event_type"] == "delivery_verifier_completed"
        )
        self.assertIn("message", str(internal["warnings"]))

    def test_agent_core_independently_rejects_forged_pass_and_scrubs_client_prose(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core-forged-pass", owner_id="analyst-1")

        def forged_workflow(request):
            package = fake_workflow(request).answer_package
            package["final_answer"] = "forged amount 42"
            package["llm_calls"] = [{"output": "secret llm prose"}]
            package["quality_gate"] = {
                "status": "passed_with_warnings",
                "errors": [{"code": "forged_error"}],
                "business_audit_summary": "secret quality prose",
            }
            package["admin_audit"] = {
                "verifier": {
                    "status": "passed_with_warnings",
                    "errors": [{"code": "forged_error"}],
                    "warnings": [{"code": "wording_risk"}],
                    "accepted_claim_indexes": [0],
                    "rejected_claim_indexes": [],
                },
                "client_note": "secret admin prose",
            }
            return WorkflowRunResult(
                status="draft",
                run_id=request["run_id"],
                answer_package=package,
                checkpoint_events=(
                    {"node": "persist_artifact", "status": "completed"},
                ),
                llm_calls=(_failed_llm_audit("delivery-verifier"),),
            )

        core = ConversationAgentCore(store, workflow_runner=forged_workflow)
        result = core.run_message(
            thread_id="thread-agent-core-forged-pass",
            run_id="run-agent-core-forged-pass",
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        package = result["answer_package"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "delivery_verifier_failed")
        self.assertNotIn("run-agent-core-forged-pass", store.answer_packages)
        self.assertNotIn("run-agent-core-forged-pass", store.analysis_runtime_records)
        self.assertEqual(package["status"], "failed")
        self.assertEqual(package["final_answer"], "")
        self.assertEqual(package["llm_calls"], [])
        self.assertEqual(
            result["llm_calls"],
            [_failed_llm_audit("delivery-verifier")],
        )
        self.assertEqual(
            store.runs["run-agent-core-forged-pass"]["checkpoint_events"],
            [{"node": "persist_artifact", "status": "completed"}],
        )
        self.assertTrue(
            any(
                event["event_type"]
                == "workflow_failure_llm_call_recorded"
                for event in store.audit_events
            )
        )
        self.assertNotIn(
            "secret",
            str({key: value for key, value in package.items() if key != "internal_audit"}),
        )
        self.assertNotIn("42", str(result["quality_review"]))

    def test_agent_core_scrubs_failed_verifier_package_before_return_and_persist(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core-scrub", owner_id="analyst-1")

        def rejected_workflow(request):
            package = fake_workflow(request).answer_package
            package["final_answer"] = "未经验证的付费金额为 42。"
            package["sections"][0]["payload"]["answer_text"] = "未经验证的付费金额为 42。"
            package["admin_audit"]["verifier"] = {
                "status": "failed",
                "errors": ({"code": "number_mismatch", "claim_index": 0},),
                "rejected_claim_indexes": (0,),
            }
            return WorkflowRunResult(
                status="draft",
                run_id=request["run_id"],
                answer_package=package,
                checkpoint_events=(),
            )

        core = ConversationAgentCore(store, workflow_runner=rejected_workflow)
        result = core.run_message(
            thread_id="thread-agent-core-scrub",
            run_id="run-agent-core-scrub",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        returned = result["answer_package"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "delivery_verifier_failed")
        self.assertEqual(returned["status"], "failed")
        self.assertEqual(returned["final_answer"], "")
        self.assertEqual(returned["sections"][0]["payload"]["answer_text"], "")
        self.assertNotIn("42", str(result["quality_review"]))
        self.assertNotIn("run-agent-core-scrub", store.answer_packages)

    def test_agent_core_runs_workflow_and_persists_answer_package(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-agent-core",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["accepted_graph"], [])
        self.assertEqual(store.runs["run-agent-core"]["status"], "completed")
        self.assertTrue(store.context_manifests)
        self.assertTrue(store.answer_packages)
        package = store.answer_packages["run-agent-core"]
        self.assertEqual(package["run_id"], "run-agent-core")
        self.assertEqual(package["status"], "draft")
        self.assertEqual(package["sections"][0]["payload"]["answer_text"], "")
        topic = store.current_topic("thread-agent-core")
        self.assertIsNotNone(store.latest_artifact_for_topic(topic.topic_id))
        self.assertTrue(
            any(event["event_type"] == "answer_package_recorded" for event in store.audit_events)
        )

    def test_agent_core_returns_context_manifest_with_current_run_evidence_refs(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core-evidence", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core-evidence",
            run_id="run-agent-core-evidence",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        refs = {
            item["source_ref"]
            for item in result["context_manifest"]["items"]
            if item.get("can_support_claims") is True
        }
        self.assertNotIn("evidence:fake-workflow", refs)
        self.assertFalse(result["context_manifest"]["can_support_claims"])

    def test_agent_core_creates_thread_before_initial_run_insert(self):
        store = StrictThreadStore()
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core-strict",
            run_id="run-agent-core-strict",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed")
        self.assertIn("thread-agent-core-strict", store.threads)

    def test_agent_core_does_not_run_langgraph_for_capability_question(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        calls = []

        def runner(_request):
            calls.append(_request)
            return fake_workflow(_request)

        core = ConversationAgentCore(store, workflow_runner=runner)
        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-capability-question",
            user_message="你现在能看哪些数据？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed_without_workflow")
        self.assertEqual(calls, [])
        self.assertIsNone(store.runs["run-capability-question"]["answer_package"])

    def test_agent_core_waits_for_structured_clarification_without_running_workflow(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        calls = []

        def runner(_request):
            calls.append(_request)
            return fake_workflow(_request)

        core = ConversationAgentCore(store, workflow_runner=runner)
        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-needs-clarification",
            user_message="这个月是不是变好了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "waiting_for_clarification")
        self.assertEqual(calls, [])
        self.assertIn("clarification", result)
        self.assertEqual(store.runs["run-needs-clarification"]["status"], "waiting_for_clarification")
        self.assertEqual(
            store.runs["run-needs-clarification"]["request"]["clarification"]["clarification_id"],
            result["clarification"]["clarification_id"],
        )
        self.assertTrue(
            any(
                event["event_type"] == "clarification_requested"
                and event["run_id"] == "run-needs-clarification"
                for event in store.audit_events
            )
        )

    def test_agent_core_failed_workflow_still_returns_context_manifest(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_failed_workflow)

        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-failed-workflow",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("context_manifest", result)
        self.assertTrue(result["context_manifest"])

    def test_agent_core_persists_failed_workflow_nodes_and_llm_audits(self):
        audits = tuple(
            {
                "task": "business_intent",
                "provider": "contract-test-provider",
                "model": "contract-test-model",
                "prompt_version": "contract-test-v1",
                "response_id": f"response-{attempt}",
                "structured_output": {
                    "question_family": family,
                    "analysis_requirements": {
                        "context_sources": ["gameplay"]
                    },
                },
                "raw_response_content": json.dumps(
                    {"question_family": family}, ensure_ascii=False
                ),
            }
            for attempt, family in enumerate(
                (
                    "pattern_explanation",
                    "anomaly_or_black_swan_review",
                    "data_quality_or_evidence_review",
                ),
                start=1,
            )
        )
        checkpoints = tuple(
            {
                "node": "understand_business_intent",
                "attempt": attempt,
                "status": status,
                "reason": "context_family_axis_missing:gameplay",
            }
            for attempt, status in enumerate(
                ("retrying", "retrying", "failed"), start=1
            )
        )

        def workflow(request):
            return WorkflowRunResult(
                status="failed",
                run_id=request["run_id"],
                failure_reason="context_family_axis_missing:gameplay",
                checkpoint_events=checkpoints,
                llm_calls=audits,
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-failed-workflow-audits",
            run_id="run-failed-workflow-audits",
            user_message="昨天玩法活跃和付费变化能对上吗？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(tuple(result["llm_calls"]), audits)
        self.assertEqual(
            store.runs["run-failed-workflow-audits"]["checkpoint_events"],
            list(checkpoints),
        )
        recorded = tuple(
            event
            for event in store.audit_events
            if event["event_type"] == "workflow_failure_llm_call_recorded"
        )
        self.assertEqual(len(recorded), 3)
        self.assertEqual(
            tuple(event["payload"] for event in recorded), audits
        )
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"] == "workflow_failed"
        )
        self.assertEqual(
            failure["payload"],
            {
                "failure_reason": "context_family_axis_missing:gameplay",
                "failure_owner": "workflow_runtime_owner",
            },
        )

    def test_agent_core_failed_workflow_retains_boundary_package_and_owner(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-boundary-failure", owner_id="analyst-1")

        def workflow(request):
            return WorkflowRunResult(
                status="failed",
                run_id=request["run_id"],
                failure_reason="delivery_reverify_failed:number_mismatch",
                answer_package={
                    "status": "failed",
                    "admin_audit": {
                        "verifier": {
                            "status": "failed",
                            "errors": [{"code": "number_mismatch"}],
                        }
                    },
                },
                artifact_path="artifacts/phase-7/boundary-failure/answer_package.json",
            )

        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-boundary-failure",
            run_id="run-boundary-failure",
            user_message="昨天付费金额为什么变化？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_owner"], "evidence_verifier_owner")
        self.assertEqual(
            result["answer_package"]["admin_audit"]["verifier"]["errors"],
            [{"code": "number_mismatch"}],
        )
        self.assertEqual(
            result["artifact_path"],
            "artifacts/phase-7/boundary-failure/answer_package.json",
        )

    def test_live_conversation_case_schema_supports_clarification_resume(self):
        from tools.phase7.run_live_conversation_system_test import load_cases

        cases = load_cases("evals/phase7/conversation_scenarios.yaml")
        case = next(item for item in cases if item["id"] == "q2_q1_wajespecial_long_followup")
        self.assertGreaterEqual(len(case["turns"]), 4)
        self.assertIs(case["turns"][3]["expect"]["allow_clarification"], True)
        self.assertIn("clarification_response", case["turns"][3])
        self.assertFalse(any("case_id" in item for item in cases))

    def test_live_conversation_cases_include_long_thread_stress_set(self):
        from tools.phase7.run_live_conversation_system_test import load_cases

        cases = load_cases("evals/phase7/conversation_scenarios.yaml")
        matches = [item for item in cases if item["id"] == "q2_q1_long_thread_stress"]
        self.assertTrue(matches)
        case = matches[0]
        required = {
            capability
            for turn in case["turns"]
            for capability in turn.get("expect", {}).get("required_capabilities", ())
        }

        self.assertEqual(case["group"], "long_thread_stress")
        self.assertGreaterEqual(len(case["turns"]), 8)
        self.assertTrue(any("clarification_response" in turn for turn in case["turns"]))
        self.assertTrue(
            {
                "driver_decomposition",
                "segment_contribution",
                "joint_attribution",
                "outlier_scan",
                "outlier_contribution",
                "answer_verify",
            }.issubset(required)
        )

    def test_live_conversation_harness_runs_clarification_and_resumes_same_topic(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import load_cases, run_case

        case = next(
            item
            for item in load_cases("evals/phase7/conversation_scenarios.yaml")
            if item["id"] == "q2_q1_wajespecial_long_followup"
        )

        with TemporaryDirectory() as tmpdir:
            result = run_case(
                ConversationAgentCore.from_environment(),
                case,
                Path(tmpdir),
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("run_id", result)
        self.assertIn("topic_id", result)
        self.assertIn("answer_package", result)
        self.assertIn("context_manifest", result)
        self.assertIn("accepted_graph", result)
        self.assertIn("llm_calls", result)
        self.assertEqual(
            result["answer_package"]["status"],
            "failed",
        )
        self.assertIn("quality_review", result)
        clarification_turn = result["turns"][3]
        second_turn = result["turns"][1]
        third_turn = result["turns"][2]
        self.assertEqual(clarification_turn["status"], "waiting_for_clarification")
        self.assertEqual(clarification_turn["resumed_status"], "failed")
        self.assertEqual(clarification_turn["topic_id"], clarification_turn["resumed_topic_id"])
        self.assertEqual(result["turns"][0]["topic_id"], result["turns"][1]["topic_id"])
        self.assertEqual(result["turns"][1]["topic_id"], result["turns"][2]["topic_id"])
        self.assertEqual(
            set(second_turn["expectation_review"]["missing_required_capabilities"]),
            {"segment_contribution", "joint_attribution"},
        )
        self.assertEqual(
            set(third_turn["expectation_review"]["missing_required_capabilities"]),
            {"joint_attribution", "answer_verify"},
        )
        for turn in result["turns"]:
            with self.subTest(turn=turn["index"]):
                self.assertTrue(turn["expectation_review"]["intent_passed"])
                self.assertTrue(turn["expectation_review"]["topic_relation_passed"])
                self.assertTrue(turn["expectation_review"]["context_manifest_present"])
                self.assertTrue(turn["expectation_review"]["context_manifest_can_support_claims"])
                self.assertFalse(turn["expectation_review"]["claim_support_policy_passed"])
                self.assertEqual(turn["expectation_review"]["claim_evidence_review"]["claim_count"], 0)
                self.assertEqual(
                    turn["expectation_review"]["claim_evidence_review"]["unsupported_evidence_refs"],
                    [],
                )
                self.assertEqual(
                    turn["expectation_review"]["missing_final_answer_text"],
                    turn["expectation_review"]["final_answer_contains"],
                )
        self.assertIsNone(clarification_turn["resumed_accepted_graph"])

    def test_agent_core_passes_clarification_answer_as_workflow_choice(self):
        captured: dict[str, object] = {}

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        store = InMemoryConversationStore()
        store.create_thread("thread-clarification-choice", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=workflow)

        first = core.run_message(
            thread_id="thread-clarification-choice",
            run_id="run-initial-choice",
            user_message="Q2 相比 Q1 付费金额为什么变了？",
        )
        captured.clear()
        waiting = core.run_message(
            thread_id="thread-clarification-choice",
            run_id="run-waiting-choice",
            user_message="如果去掉异常天还成立吗？",
        )
        resumed = core.run_message(
            thread_id="thread-clarification-choice",
            run_id="run-resumed-choice",
            user_message="按日粒度，移除贡献最大的正向日期后复算，不做订单级明细剔除。",
        )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(waiting["status"], "waiting_for_clarification")
        self.assertEqual(resumed["status"], "completed")
        self.assertIn("clarification_choice", captured)
        self.assertEqual(captured["clarification_choice"]["outlier_removal_strategy"], "daily_remove_top_positive_day")
        self.assertIn("answer_text", captured["clarification_choice"])

    def test_agent_core_passes_row_provider_to_workflow_request(self):
        captured: dict[str, object] = {}
        provider = object()

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        store = InMemoryConversationStore()
        store.create_thread("thread-row-provider", owner_id="analyst-1")
        core = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            row_provider=provider,
        )

        result = core.run_message(
            thread_id="thread-row-provider",
            run_id="run-row-provider",
            user_message="昨天付费金额为什么上涨/下跌？",
        )

        self.assertEqual(result["status"], "completed")
        self.assertIs(captured["row_provider"], provider)

    def test_agent_core_passes_prior_assets_to_workflow_request(self):
        captured: dict[str, object] = {}

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        store = InMemoryConversationStore()
        store.create_thread("thread-prior-assets", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=workflow)

        result = core.run_message(
            thread_id="thread-prior-assets",
            run_id="run-prior-assets",
            user_message="继续看哪个渠道影响最大",
            prior_analysis_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            captured["prior_analysis_assets"],
            (
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "dimensions": ["channel"],
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

    def test_main_accepts_prior_analysis_assets_argument(self):
        captured: dict[str, object] = {}

        def fake_run_message(self, **kwargs):
            captured.update(kwargs)
            return {"status": "completed"}

        store = InMemoryConversationStore()
        output = StringIO()
        argv = [
            "--thread-id",
            "thread-cli-prior-assets",
            "--run-id",
            "run-cli-prior-assets",
            "--message",
            "继续看哪个渠道影响最大",
            "--prior-analysis-assets",
            json.dumps(
                [
                    {
                        "asset_type": "dimension_scan",
                        "dimension": "channel",
                        "status": "usable",
                        "query_ref": "query:channel-scan",
                    }
                ],
                ensure_ascii=False,
            ),
        ]

        with patch(
            "bi_agent.conversation.agent_core.PostgresConversationStore.from_env",
            return_value=store,
        ), patch(
            "bi_agent.conversation.agent_core._conversation_llm_from_env",
            return_value=None,
        ), patch.object(
            ConversationAgentCore,
            "run_message",
            fake_run_message,
        ), patch(
            "sys.stdout",
            output,
        ):
            exit_code = __import__("bi_agent.conversation.agent_core", fromlist=["main"]).main(argv)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            captured["prior_analysis_assets"],
            (
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

    def test_agent_core_persists_json_safe_request_when_row_provider_is_object(self):
        captured: dict[str, object] = {}
        provider = object()

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        store = JsonStrictStore()
        store.create_thread("thread-row-provider-json", owner_id="analyst-1")
        core = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            row_provider=provider,
        )

        result = core.run_message(
            thread_id="thread-row-provider-json",
            run_id="run-row-provider-json",
            user_message="昨天付费金额为什么上涨/下跌？",
        )

        self.assertEqual(result["status"], "completed")
        self.assertIs(captured["row_provider"], provider)
        self.assertEqual(
            store.runs["run-row-provider-json"]["request"]["row_provider"]["type"],
            "object",
        )

    def test_agent_core_from_environment_real_clickhouse_configures_analysis_runtime(self):
        from bi_agent.runtime.analysis_runtime import AnalysisRuntime

        with patch(
            "bi_agent.conversation.agent_core.PostgresConversationStore.from_env",
            return_value=InMemoryConversationStore(),
        ):
            core = ConversationAgentCore.from_environment(real_clickhouse=True)

        self.assertIsInstance(core.analysis_runtime, AnalysisRuntime)
        self.assertIs(core.evidence_resolver, core.analysis_runtime.evidence_resolver)

    def test_real_clickhouse_core_refreshes_trusted_release_snapshots_per_plan(self):
        from tests.phase7.test_conversation_persistence import (
            _release_ref,
            _release_snapshot_payload,
        )

        store = InMemoryConversationStore()
        with patch(
            "bi_agent.conversation.agent_core.PostgresConversationStore.from_env",
            return_value=store,
        ):
            core = ConversationAgentCore.from_environment(real_clickhouse=True)

        self.assertIs(core.analysis_runtime.release_resolver, store)
        self.assertIs(core.analysis_runtime.executor.release_resolver, store)
        self.assertIs(
            core.analysis_runtime.executor.evidence_resolver,
            core.evidence_resolver,
        )
        self.assertIs(core.analysis_runtime.executor.rows_loader, core.rows_loader)

        seen_refs = []
        for revision in ("load:dynamic-v1", "load:dynamic-v2"):
            payloads = (
                _release_snapshot_payload(
                    f"snapshot:dynamic-overall:{revision}",
                    "market_dashboard",
                    revision=revision,
                ),
                _release_snapshot_payload(
                    f"snapshot:dynamic-channel:{revision}",
                    "market_dashboard_channel",
                    revision=revision,
                ),
            )
            release_ref = _release_ref(payloads)
            for payload in payloads:
                payload["release_ref"] = release_ref
            store.publish_dataset_snapshot_release(
                release_ref=release_ref,
                logical_snapshot_id="dashboard-logical",
                payloads=payloads,
            )
            snapshot_ref = payloads[0]["snapshot_ref"]
            catalog_refs = {
                item.snapshot_ref
                for item in core.analysis_runtime._active_catalog().snapshots()
            }
            self.assertIn(snapshot_ref, catalog_refs)
            seen_refs.append(snapshot_ref)

        self.assertNotEqual(*seen_refs)
        channel_ref = payloads[1]["snapshot_ref"]
        self.assertIn(
            channel_ref,
            {
                item.snapshot_ref
                for item in core.analysis_runtime._active_catalog().snapshots()
            },
        )

    def test_follow_up_hints_route_user_mix_and_high_value_capabilities(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-follow-up-hints", owner_id="analyst-1")
        runtime.handle_message("thread-follow-up-hints", "Q2 相比 Q1 付费金额为什么变了？")

        result = runtime.handle_message(
            "thread-follow-up-hints",
            "新老用户和高价值用户的用户质量分别怎么看？",
        )

        self.assertIn("user_mix_contribution", result.run_request.requested_nodes)
        self.assertIn("high_value_user_contribution", result.run_request.requested_nodes)

    def test_revenue_diagnostic_language_routes_to_general_capabilities(self):
        cases = (
            (
                "最近付费金额是否存在固定规律，比如周末更高、月初更高、晚上更高？"
                "这个规律主要由哪个渠道、地区、用户类型或玩法带动？",
                {"pattern_scan", "segment_contribution", "joint_attribution", "answer_verify"},
            ),
            (
                "当前收入健康吗？是靠正常用户增长带动，还是靠少数大额用户、"
                "短期活动或异常渠道拉动？收入结构里最大的风险点是什么？",
                {
                    "driver_decomposition",
                    "user_mix_contribution",
                    "high_value_user_contribution",
                    "outlier_scan",
                    "data_quality_profile",
                    "event_evidence",
                    "answer_verify",
                },
            ),
            (
                "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？"
                "哪些指标偏离了正常水平？",
                {
                    "compare_periods",
                    "rolling_window_compare",
                    "driver_decomposition",
                    "answer_verify",
                },
            ),
            (
                "这个结论的数据证据够不够？是否存在数据延迟、渠道归因异常、"
                "支付状态缺失、重复订单或异常用户影响判断？",
                {
                    "data_quality_profile",
                    "segment_contribution",
                    "joint_attribution",
                    "outlier_scan",
                    "answer_verify",
                },
            ),
        )

        for message, expected_nodes in cases:
            with self.subTest(message=message):
                store = InMemoryConversationStore()
                runtime = ConversationRuntime(store)
                store.create_thread("thread-revenue-diagnostics", owner_id="analyst-1")
                runtime.handle_message(
                    "thread-revenue-diagnostics",
                    "昨天付费金额为什么变化？",
                )

                result = runtime.handle_message("thread-revenue-diagnostics", message)

                self.assertIsNotNone(result.run_request)
                self.assertTrue(expected_nodes.issubset(set(result.run_request.requested_nodes)))

    def test_first_business_question_creates_topic_then_time_wording_inherits(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-revenue-topic-reuse", owner_id="analyst-1")

        first = runtime.handle_message(
            "thread-revenue-topic-reuse",
            "昨天付费金额为什么上涨/下跌？",
        )
        second = runtime.handle_message(
            "thread-revenue-topic-reuse",
            "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？",
        )

        self.assertEqual(first.turn_intent.intent, "new_topic")
        self.assertEqual(first.topic_relation, "new_topic")
        self.assertEqual(second.turn_intent.intent, "follow_up")
        self.assertEqual(second.topic_relation, "inherit_current")
        self.assertEqual(first.topic_id, second.topic_id)

    def test_live_conversation_cases_include_revenue_diagnostic_question_set(self):
        from tools.phase7.run_live_conversation_system_test import load_cases

        expected_questions = [
            "昨天付费金额为什么上涨/下跌？主要是首充人数、付费频次、单笔付费金额，还是支付成功率等因素变化导致的",
            "最近付费金额是否存在固定规律，比如周末更高、月初更高、晚上更高？这个规律主要由哪个渠道、地区、用户类型或玩法带动？",
            "昨天的活动、投放预算、素材更换、版本更新、支付通道、节日或外部事件，是否影响了付费金额？",
            "当前收入健康吗？是靠正常用户增长带动，还是靠少数大额用户、短期活动或异常渠道拉动？收入结构里最大的风险点是什么？",
            "昨天收入变化最大的是哪个一级渠道、地区、设备、包、支付方式或玩法？对收入影响最大的 3 个因子分别是什么？",
            "昨天有没有异常波动？如果有，是哪个渠道、支付通道、地区、设备、玩法或大额用户造成的？",
            "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？哪些指标偏离了正常水平？",
            "这个结论的数据证据够不够？是否存在数据延迟、渠道归因异常、支付状态缺失、重复订单或异常用户影响判断？",
        ]
        cases = load_cases("evals/phase7/conversation_scenarios.yaml")
        case = next(
            item
            for item in cases
            if item["id"] == "paid_amount_revenue_diagnostics_8_question_set"
        )

        self.assertEqual(case["group"], "production_revenue_diagnostics")
        self.assertEqual(
            case["analysis_context"],
            {
                "as_of": "2026-06-03T12:00:00+01:00",
                "target_date": "2026-06-02",
                "previous_day": "2026-06-01",
                "rolling_7_day_start": "2026-05-26",
                "rolling_7_day_end": "2026-06-01",
                "same_weekday_last_week": "2026-05-26",
                "pattern_history_start": "2026-01-01",
                "anomaly_history_start": "2026-05-03",
            },
        )
        self.assertEqual(
            case["required_datasets"],
            [
                "paid_order_success",
                "payment_attempt",
                "market_dashboard",
                "gameplay",
                "external_event",
            ],
        )
        self.assertEqual([turn["user"] for turn in case["turns"]], expected_questions)
        self.assertEqual(case["turns"][0]["expect"]["topic_relation"], "create")
        self.assertTrue(
            all(turn["expect"]["topic_relation"] == "inherit" for turn in case["turns"][1:])
        )
        self.assertTrue(
            all(turn["scenario"].get("required_capabilities") for turn in case["turns"])
        )
        self.assertTrue(
            all("major_nodes" not in turn["expect"] for turn in case["turns"])
        )

    def test_live_harness_rejects_claim_refs_without_traceable_source(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["artifact:missing"],
                                    }
                                ],
                            }
                        },
                        {
                            "payload": {
                                "evidence": [
                                    {
                                        "evidence_ref": "artifact:missing",
                                        "evidence_type": "artifact",
                                    }
                                ]
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "can_support_claims": True,
                    "items": [],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(
            review["claim_evidence_review"]["unsupported_evidence_refs"],
            ["artifact:missing"],
        )

    def test_live_harness_accepts_current_run_evidence_manifest_refs_without_prefix(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["coverage_block:run-1"],
                                        "context_manifest_ref": "manifest-coverage-block",
                                        "reuse_decisions": [
                                            {
                                                "decision": "rerun",
                                                "result_ref": "",
                                                "reason": "current_run_evidence",
                                            }
                                        ],
                                    }
                                ],
                            }
                        },
                        {
                            "payload": {
                                "evidence": [
                                    {
                                        "evidence_ref": "coverage_block:run-1",
                                        "evidence_type": "insufficient",
                                    }
                                ]
                            }
                        },
                    ]
                },
                "context_manifest": {
                    "manifest_id": "manifest-coverage-block",
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "coverage_block:run-1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertTrue(review["claim_support_policy_passed"])
        self.assertEqual(review["claim_evidence_review"]["unsupported_evidence_refs"], [])
        self.assertTrue(review["passed"])

    def test_live_harness_rejects_context_only_manifest_refs_for_claims(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["topic-1"],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "can_support_claims": False,
                    "items": [
                        {
                            "source_ref": "topic-1",
                            "can_support_claims": False,
                            "claim_use": "context_only",
                        }
                    ],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(
            review["claim_evidence_review"]["unsupported_evidence_refs"],
            ["topic-1"],
        )

    def test_live_harness_rejects_topic_refs_even_when_marked_claim_supporting(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["topic-1"],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "topic",
                            "source_ref": "topic-1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(
            review["claim_evidence_review"]["unsupported_evidence_refs"],
            ["topic-1"],
        )

    def test_live_harness_rejects_claims_missing_manifest_or_reuse_decision(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["evidence:1"],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "manifest_id": "context-1",
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "evidence:1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(review["claim_evidence_review"]["missing_context_manifest_ref"], [0])
        self.assertEqual(review["claim_evidence_review"]["missing_reuse_decision_indexes"], [0])

    def test_live_harness_marks_real_clickhouse_unverified_without_clickhouse_refs(self):
        from tools.phase7.run_live_conversation_system_test import _real_clickhouse_review

        review = _real_clickhouse_review(
            {
                "answer_package": {
                    "sections": [
                        {
                            "section_id": "evidence",
                            "payload": {
                                "evidence": [{"result_refs": ["fixture-hash"]}]
                            },
                        }
                    ]
                }
            },
            real_clickhouse=True,
        )

        self.assertFalse(review["real_clickhouse_verified"])
        self.assertIn("missing_clickhouse_result_refs", review["issues"])

    def test_live_harness_passes_fixed_analysis_context_on_clarification_resume(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import run_case

        calls = []
        manifest = {
            "manifest_id": "context-fixed",
            "can_support_claims": True,
            "items": [],
        }

        class Core:
            evidence_resolver = None

            def run_message(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return {
                        "status": "waiting_for_clarification",
                        "run_id": "run-fixed-1",
                        "topic_id": "topic-fixed",
                        "intent": "new_topic",
                        "topic_relation": "new_topic",
                        "context_manifest": manifest,
                    }
                return {
                    "status": "completed",
                    "run_id": "run-fixed-2",
                    "topic_id": "topic-fixed",
                    "intent": "follow_up",
                    "topic_relation": "inherit_current",
                    "answer_package": {"sections": []},
                    "context_manifest": manifest,
                    "accepted_graph": [],
                    "llm_calls": [],
                }

        fixed = {"as_of": "2026-06-03T12:00:00+01:00"}
        case = {
            "id": "fixed-context-resume",
            "analysis_context": fixed,
            "turns": [{
                "user": "昨天付费金额为什么变化？",
                "clarification_response": "按推荐继续。",
                "expect": {"allow_clarification": True},
            }],
        }
        with TemporaryDirectory() as tmpdir:
            run_case(Core(), case, Path(tmpdir))

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["analysis_context"], fixed)
        self.assertEqual(calls[1]["analysis_context"], fixed)

    def test_live_harness_accepts_recommended_clarification_without_fixture_text(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import run_case

        calls = []
        manifest = {
            "manifest_id": "context-auto-clarification",
            "can_support_claims": True,
            "items": [],
        }

        class Core:
            evidence_resolver = None

            def run_message(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return {
                        "status": "waiting_for_clarification",
                        "run_id": "run-auto-1",
                        "topic_id": "topic-auto",
                        "intent": "new_topic",
                        "topic_relation": "new_topic",
                        "context_manifest": manifest,
                        "clarification": {
                            "recommended_choice_id": "choice-supported",
                            "recommended_assumption": {
                                "option": "使用受支持口径继续"
                            },
                            "choice_actions": [{
                                "choice_id": "choice-supported",
                                "business_label": "使用受支持口径继续",
                                "action_kind": "choose_supported_contract",
                            }],
                        },
                    }
                return {
                    "status": "completed",
                    "run_id": "run-auto-2",
                    "topic_id": "topic-auto",
                    "intent": "clarification_answer",
                    "topic_relation": "inherit_current",
                    "answer_package": {"final_answer": "已按受支持口径完成。", "sections": []},
                    "context_manifest": manifest,
                    "accepted_graph": [],
                    "llm_calls": [],
                }

        case = {
            "id": "auto-clarification",
            "analysis_context": {"as_of": "2026-06-03T12:00:00+01:00"},
            "turns": [{
                "user": "昨天付费金额为什么变化？",
                "expect": {"allow_clarification": True},
            }],
        }
        with TemporaryDirectory() as tmpdir:
            output = run_case(Core(), case, Path(tmpdir))

        turn = output["turns"][0]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["user_message"], "使用受支持口径继续")
        self.assertEqual(turn["clarification_response"], "使用受支持口径继续")
        self.assertEqual(turn["resumed_status"], "completed")
        self.assertEqual(turn["topic_id"], turn["resumed_topic_id"])

    def test_live_harness_prefers_progressing_contract_action_over_wait(self):
        from tools.phase7.run_live_conversation_system_test import (
            _automatic_clarification_response,
        )

        response = _automatic_clarification_response({
            "clarification": {
                "recommended_choice_id": "wait",
                "choice_actions": [
                    {
                        "choice_id": "omit",
                        "action_kind": "omit_unavailable_context",
                        "business_label": "继续主指标分析并保留缺口",
                    },
                    {
                        "choice_id": "wait",
                        "action_kind": "wait_for_source",
                        "business_label": "等待数据源",
                    },
                ],
            }
        })

        self.assertEqual(response, "继续主指标分析并保留缺口")

    def test_live_harness_uses_exact_recommended_option_without_typed_actions(self):
        from tools.phase7.run_live_conversation_system_test import (
            _automatic_clarification_response,
        )

        response = _automatic_clarification_response({
            "clarification": {
                "recommended_assumption": {
                    "option": "按已设定的基线继续分析"
                },
                "questions": [{
                    "question": "请选择基线",
                    "options": [
                        "按已设定的基线继续分析",
                        "改用其他基线",
                        "tell the agent to do differently",
                    ],
                }],
            }
        })

        self.assertEqual(response, "按已设定的基线继续分析")

    def test_live_harness_rejects_partial_authoritative_query_result(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-partial-authority-review",
        )
        base = context["evidence_resolver"]
        binding = base.resolve_capability_binding(context["binding_manifest_ref"])
        partial_ref = binding.completeness_record_refs[0]

        class PartialResolver:
            def __getattr__(self, name):
                return getattr(base, name)

            def resolve_completeness(self, ref):
                record = base.resolve_completeness(ref)
                if ref != partial_ref:
                    return record
                return replace(
                    record,
                    report_payload={
                        **dict(record.report_payload),
                        "completeness_status": "partial",
                        "analysis_readiness": "blocked",
                    },
                )

        review = _real_clickhouse_review(
            self._persisted_runtime_result(
                package,
                context_manifest={
                    "manifest_id": "context-manifest:partial",
                    "can_support_claims": True,
                    "sources": [],
                },
            ),
            real_clickhouse=True,
            evidence_resolver=PartialResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
        )

        query_ref = binding.query_contract_refs[0]
        self.assertFalse(review["real_clickhouse_verified"])
        self.assertIn(
            f"incomplete_clickhouse_query:{query_ref}",
            review["issues"],
        )

    def test_live_harness_accepts_partial_when_persisted_slot_contract_allows_it(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-contract-partial-authority-review",
        )
        package["verified_claims"] = []
        for section in package.get("sections", []):
            payload = section.get("payload") or {}
            if "claims" in payload:
                payload["claims"] = []
        base = context["evidence_resolver"]
        binding = base.resolve_capability_binding(context["binding_manifest_ref"])
        completeness_ref = binding.completeness_record_refs[0]
        plan_payload = canonical_value(binding.plan_payload)
        plan_payload["minimum_readiness"]["accepted_completeness"] = [
            "complete",
            "partial",
        ]
        for slot in plan_payload["required_input_slots"]:
            slot["accepted_completeness"] = ["complete", "partial"]
        degraded_binding = replace(
            binding,
            status="degraded",
            input_completeness_statuses=("partial",),
            plan_payload=plan_payload,
        )

        class ContractPartialResolver:
            def __getattr__(self, name):
                return getattr(base, name)

            def resolve_capability_binding(self, ref):
                if ref == binding.record_ref:
                    return degraded_binding
                return base.resolve_capability_binding(ref)

            def resolve_completeness(self, ref):
                record = base.resolve_completeness(ref)
                if ref != completeness_ref:
                    return record
                payload = canonical_value(record.report_payload)
                payload.update({
                    "completeness_status": "partial",
                    "analysis_readiness": "degraded",
                    "assertion_results": [
                        {
                            "assertion": "execution_succeeded",
                            "passed": True,
                            "failure_reasons": [],
                            "details": {},
                        },
                        {
                            "assertion": "data_quality_warning",
                            "passed": False,
                            "failure_reasons": ["null_bucket_present"],
                            "details": {},
                        },
                    ],
                    "failure_reasons": ["null_bucket_present"],
                })
                return replace(record, report_payload=payload)

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest={}),
            real_clickhouse=True,
            evidence_resolver=ContractPartialResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
        )

        self.assertTrue(review["real_clickhouse_verified"], review["issues"])
        self.assertTrue(
            review["runtime_correctness"]["all_required_queries_complete"]
        )

    def test_live_harness_rejects_shifted_pattern_history_window(self):
        from bi_agent.runtime.analysis_contracts import ResolvedWindow
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-shifted-pattern-history",
        )
        base = context["evidence_resolver"]
        binding = base.resolve_capability_binding(context["binding_manifest_ref"])
        result_ref = binding.result_refs[0]

        class ShiftedResolver:
            def __getattr__(self, name):
                return getattr(base, name)

            def resolve_query_execution(self, ref):
                record = base.resolve_query_execution(ref)
                if ref != result_ref:
                    return record
                shifted = ResolvedWindow(
                    window_id="pattern_history",
                    role="reference",
                    label="2026-02-01..2026-06-02",
                    start_inclusive="2026-02-01",
                    end_exclusive="2026-06-03",
                    timezone="Africa/Lagos",
                    aggregation="daily_series",
                    required_complete_days=122,
                    source_watermark_requirement="2026-06-02",
                )
                contract = replace(
                    record.contract,
                    window_refs=(*record.contract.window_refs, "pattern_history"),
                    resolved_windows=(*record.contract.resolved_windows, shifted),
                )
                return replace(record, contract=contract)

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest={}),
            real_clickhouse=True,
            evidence_resolver=ShiftedResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={
                "target_date": "2026-06-02",
                "pattern_history_start": "2026-01-01",
            },
        )

        self.assertIn(
            f"fixed_window_mismatch:{binding.query_contract_refs[0]}:pattern_history",
            review["issues"],
        )

    def test_live_harness_accepts_complete_runtime_authority_and_sources_manifest(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-complete-authority-review",
        )
        binding = context["evidence_resolver"].resolve_capability_binding(
            context["binding_manifest_ref"]
        )
        evidence_ref = package["sections"][1]["payload"]["evidence"][0][
            "evidence_ref"
        ]
        manifest = package["admin_audit"]["context_manifest"]
        verified_claim = package["admin_audit"]["verified_claims"][0]
        provenance = package["admin_audit"][
            "trusted_claim_provenance_records"
        ][0]
        package["verified_claims"] = [verified_claim]

        class ClaimResolver:
            def __getattr__(self, name):
                return getattr(context["evidence_resolver"], name)

            def resolve_verified_claim(self, claim_ref):
                return verified_claim if claim_ref == verified_claim["claim_ref"] else None

            def resolve_claim_provenance(self, record_ref):
                return provenance if record_ref == provenance["record_ref"] else None

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest=manifest),
            real_clickhouse=True,
            evidence_resolver=ClaimResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
        )

        self.assertTrue(review["real_clickhouse_verified"], review["issues"])
        self.assertEqual(
            review["runtime_correctness"],
            {
                "all_required_queries_complete": True,
                "all_capabilities_bound": True,
                "all_claims_traceable": True,
            },
        )

    def test_live_harness_rejects_unpersisted_verified_claim_identity(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-unpersisted-claim-review",
        )
        manifest = package["admin_audit"]["context_manifest"]
        forged = dict(package["admin_audit"]["verified_claims"][0])
        forged.pop("claim_digest")
        forged.pop("provenance_record_ref")
        package["verified_claims"] = [forged]

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest=manifest),
            real_clickhouse=True,
            evidence_resolver=context["evidence_resolver"],
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
        )

        self.assertFalse(review["runtime_correctness"]["all_claims_traceable"])
        self.assertIn(
            f"untraceable_verified_claim:{forged['claim_ref']}",
            review["issues"],
        )

    def test_live_harness_rejects_malformed_verified_claim_as_hard_failure(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-malformed-claim-review",
        )
        package["verified_claims"] = [None]

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest={}),
            real_clickhouse=True,
            evidence_resolver=context["evidence_resolver"],
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
        )

        self.assertFalse(review["real_clickhouse_verified"])
        self.assertFalse(review["runtime_correctness"]["all_claims_traceable"])
        self.assertIn("malformed_verified_claim:0", review["issues"])

    def test_live_harness_aggregate_requires_all_runtime_correctness_dimensions(self):
        from tools.phase7.run_live_conversation_system_test import (
            _aggregate_real_clickhouse_review,
        )

        aggregate = _aggregate_real_clickhouse_review(
            [{
                "real_clickhouse_review": {
                    "real_clickhouse_verified": True,
                    "clickhouse_result_refs": ["result:1"],
                    "observed_datasets": ["paid_order_success"],
                    "issues": [],
                    "runtime_correctness": {
                        "all_required_queries_complete": True,
                        "all_capabilities_bound": True,
                        "all_claims_traceable": False,
                    },
                }
            }],
            True,
            ("paid_order_success",),
        )

        self.assertFalse(aggregate["real_clickhouse_verified"])
        self.assertFalse(aggregate["runtime_correctness"]["all_claims_traceable"])

    def test_live_harness_rejects_legacy_authority_result_ref(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-legacy-authority-review",
        )
        base = context["evidence_resolver"]
        binding = base.resolve_capability_binding(context["binding_manifest_ref"])
        original_ref = binding.result_refs[0]
        legacy_ref = "legacy-hash"
        legacy_binding = replace(
            binding,
            result_refs=(legacy_ref,),
        )

        class LegacyResolver:
            def __getattr__(self, name):
                return getattr(base, name)

            def resolve_capability_binding(self, ref):
                return legacy_binding if ref == binding.record_ref else None

            def resolve_query_execution(self, ref):
                record = base.resolve_query_execution(original_ref)
                return replace(record, result_ref=legacy_ref) if ref == legacy_ref else None

            def resolve_completeness(self, ref):
                record = base.resolve_completeness(ref)
                if ref != binding.completeness_record_refs[0]:
                    return record
                return replace(record, result_ref=legacy_ref)

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest={}),
            real_clickhouse=True,
            evidence_resolver=LegacyResolver(),
        )

        self.assertFalse(review["real_clickhouse_verified"])
        self.assertIn(
            f"legacy_clickhouse_result_ref:{legacy_ref}",
            review["issues"],
        )

    def test_live_harness_does_not_trust_client_validator_and_refs(self):
        from tools.phase7.run_live_conversation_system_test import _real_clickhouse_review

        review = _real_clickhouse_review(
            {
                "answer_package": {
                    "sections": [
                        {
                            "section_id": "evidence",
                            "payload": {
                                "evidence": [{"result_refs": ["hash-real"]}]
                            },
                        }
                    ],
                    "admin_audit": {
                        "validator_results": [
                            {
                                "validator": "clickhouse_runtime",
                                "ok": True,
                                "reason": "provider_rows_loaded",
                            }
                        ]
                    },
                }
            },
            real_clickhouse=True,
        )

        self.assertFalse(review["real_clickhouse_verified"])
        self.assertIn("missing_runtime_authority_resolver", review["issues"])
        self.assertEqual(review["clickhouse_result_refs"], [])

    def test_live_harness_checks_each_clickhouse_query_intent_executed(self):
        from tools.phase7.run_live_conversation_system_test import _real_clickhouse_review

        review = _real_clickhouse_review(
            {
                "answer_package": {
                    "sections": [
                        {
                            "section_id": "evidence",
                            "payload": {
                                "evidence": [{"result_refs": ["hash-real"]}]
                            },
                        }
                    ],
                    "admin_audit": {
                        "validator_results": [
                            {
                                "validator": "clickhouse_runtime",
                                "ok": True,
                                "reason": "provider_rows_loaded",
                            }
                        ],
                        "row_query_plan": {
                            "query_plans": [
                                {
                                    "query_intent": "daily_metric_baselines",
                                    "sql_text": "SELECT 1",
                                },
                                {
                                    "query_intent": "dimension_scan",
                                    "sql_text": "SELECT 2",
                                },
                            ],
                            "query_results": [
                                {"intent": "daily_metric_baselines"},
                            ],
                            "result_refs_by_intent": {
                                "daily_metric_baselines": ["hash-real"],
                            },
                        },
                    },
                }
            },
            real_clickhouse=True,
        )

        self.assertFalse(review["real_clickhouse_verified"])
        self.assertIn(
            "missing_runtime_authority_resolver",
            review["issues"],
        )

    def test_live_harness_fails_real_clickhouse_case_without_verified_refs(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import run_case

        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)
        case = {
            "id": "real_clickhouse_requires_refs",
            "turns": [{"user": "Q2 比 Q1 付费金额为什么变了？", "expect": {}}],
        }

        with TemporaryDirectory() as tmpdir:
            result = run_case(
                core,
                case,
                Path(tmpdir),
                real_clickhouse=True,
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["real_clickhouse_review"]["real_clickhouse_verified"])
        self.assertFalse(result["turns"][0]["real_clickhouse_review"]["real_clickhouse_verified"])

    def test_live_harness_preserves_failure_llm_audits_without_artifact(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import run_case

        audits = [
            {
                "task": "business_intent",
                "provider": "contract-test-provider",
                "model": "contract-test-model",
                "prompt_version": "contract-test-v1",
                "response_id": f"response-{attempt}",
                "structured_output": {"question_family": family},
                "raw_response_content": json.dumps(
                    {"question_family": family}, ensure_ascii=False
                ),
            }
            for attempt, family in enumerate(
                (
                    "pattern_explanation",
                    "anomaly_or_black_swan_review",
                    "data_quality_or_evidence_review",
                ),
                start=1,
            )
        ]

        class Core:
            evidence_resolver = None

            def run_message(self, **kwargs):
                return {
                    "status": "failed",
                    "run_id": "run-harness-failed-audits",
                    "topic_id": "topic-harness-failed-audits",
                    "intent": "new_topic",
                    "topic_relation": "new_topic",
                    "failure_reason": "context_family_axis_missing:gameplay",
                    "answer_package": None,
                    "context_manifest": {
                        "manifest_id": "context-harness-failed-audits",
                        "can_support_claims": False,
                        "items": [],
                    },
                    "accepted_graph": [],
                    "artifact_path": "",
                    "llm_calls": audits,
                }

        case = {
            "id": "failure-audits-without-artifact",
            "turns": [
                {
                    "user": "昨天玩法活跃和付费变化能对上吗？",
                    "expect": {},
                }
            ],
        }
        with TemporaryDirectory() as tmpdir:
            output = run_case(Core(), case, Path(tmpdir))

        self.assertEqual(output["turns"][0]["status"], "failed")
        self.assertEqual(output["turns"][0]["artifact_path"], "")
        self.assertEqual(output["turns"][0]["llm_calls"], audits)
        self.assertEqual(output["llm_calls"], audits)

    def test_live_harness_uses_internal_artifact_only_for_runtime_audit(self):
        from tools.phase7.run_live_conversation_system_test import (
            _runtime_audit_package,
        )

        package, _, _ = _verified_delivery_package(
            run_id="run-internal-audit",
        )
        package["final_answer"] = "内部完整答案"
        result = self._persisted_runtime_result(package)
        result["answer_package"] = {
            "run_id": "run-internal-audit",
            "final_answer": "客户端安全答案",
            "sections": [],
        }

        audited = _runtime_audit_package(result)

        self.assertEqual(audited["final_answer"], "内部完整答案")
        self.assertEqual(
            audited["sections"][1]["payload"]["evidence"][0]["evidence_ref"],
            "segment:authoritative",
        )
        self.assertEqual(
            result["answer_package"]["final_answer"],
            "客户端安全答案",
        )

    def test_live_harness_writes_partial_case_artifact_after_each_turn(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import run_case

        calls = {"count": 0}

        def workflow(request):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("second_turn_stopped")
            return fake_workflow(request)

        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=workflow)
        case = {
            "id": "partial_turn_artifact",
            "turns": [
                {"user": "Q2 比 Q1 付费金额为什么变了？", "expect": {}},
                {"user": "继续看渠道。", "expect": {}},
            ],
        }

        with TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "partial_turn_artifact.json"
            with self.assertRaisesRegex(RuntimeError, "second_turn_stopped"):
                run_case(core, case, Path(tmpdir))
            data = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertEqual(data["status"], "running")
        self.assertEqual(len(data["turns"]), 1)
        self.assertEqual(data["turns"][0]["status"], "completed")

    def test_live_harness_loads_local_env_without_overriding_shell(self):
        import os
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import load_env_file

        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "WAJE_RUNTIME_DATABASE_URL=postgres://local\nWAJE_LLM_MODEL=deepseek-chat\n",
                encoding="utf-8",
            )
            old_database_url = os.environ.pop("WAJE_RUNTIME_DATABASE_URL", None)
            old_model = os.environ.get("WAJE_LLM_MODEL")
            os.environ["WAJE_LLM_MODEL"] = "shell-model"
            try:
                loaded = load_env_file(str(env_path))
                self.assertIn("WAJE_RUNTIME_DATABASE_URL", loaded)
                self.assertNotIn("WAJE_LLM_MODEL", loaded)
                self.assertEqual(os.environ["WAJE_RUNTIME_DATABASE_URL"], "postgres://local")
                self.assertEqual(os.environ["WAJE_LLM_MODEL"], "shell-model")
            finally:
                if old_database_url is None:
                    os.environ.pop("WAJE_RUNTIME_DATABASE_URL", None)
                else:
                    os.environ["WAJE_RUNTIME_DATABASE_URL"] = old_database_url
                if old_model is None:
                    os.environ.pop("WAJE_LLM_MODEL", None)
                else:
                    os.environ["WAJE_LLM_MODEL"] = old_model

    def test_live_harness_reports_quality_gate_without_blocking_acceptance(self):
        from tools.phase7.run_live_conversation_system_test import (
            _strict_quality_failed,
            _quality_review,
        )

        review = _quality_review(
            {
                "quality_gate": {
                    "blocks_display": False,
                    "display_status": "ready_with_warnings",
                    "repairable_warnings": ["missing_business_interpretation"],
                    "direct_answer": True,
                    "has_verified_claims": True,
                    "verified_claim_preserved": False,
                    "business_insight_present": True,
                    "followups_one_intent": False,
                    "issues": ["missing_verified_claim"],
                    "risk_flags": ["causal_wording_risk"],
                }
            }
        )

        self.assertEqual(
            review,
            {
                "blocks_display": False,
                "display_status": "ready_with_warnings",
                "final_answer_audit_warnings": ["missing_business_interpretation"],
                "quality_gate_issues": ["missing_verified_claim"],
                "final_summary_display_warnings": [],
                "quality_warnings": [
                    "missing_verified_claim",
                    "missing_business_interpretation",
                ],
                "risk_markers": ["causal_wording_risk"],
                "direct_answer": True,
                "has_verified_claims": True,
                "verified_claim_preserved": False,
                "business_insight_present": True,
                "followups_one_intent": False,
            },
        )
        self.assertFalse(_strict_quality_failed({"quality_review": review}))
        self.assertFalse(
            _strict_quality_failed({"quality_review": {**review, "blocks_display": True}})
        )

    def test_eval_review_tool_emits_nonblocking_runtime_and_quality_scorecards(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.review_analysis_contract_eval import review_artifact

        artifact = {
            "case_id": "fixed-review",
            "turns": [{
                "index": 1,
                "real_clickhouse_review": {
                    "runtime_correctness": {
                        "all_required_queries_complete": True,
                        "all_capabilities_bound": True,
                        "all_claims_traceable": True,
                    },
                    "issues": [],
                },
                "quality_review": {
                    "direct_answer": True,
                    "business_insight_present": True,
                    "followups_one_intent": False,
                    "has_verified_claims": True,
                    "verified_claim_preserved": True,
                    "quality_warnings": ["weak_followup"],
                    "risk_markers": ["causal_wording_risk"],
                },
                "answer_package": {"final_answer": "已验证结论。"},
            }],
        }
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "artifact.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            review = review_artifact(path)

        self.assertEqual(
            review["runtime_correctness"],
            {
                "all_required_queries_complete": True,
                "all_capabilities_bound": True,
                "all_claims_traceable": True,
            },
        )
        self.assertEqual(
            set(review["answer_quality"]),
            {
                "directness",
                "insight",
                "actionability",
                "evidence_discipline",
                "risk_markers",
            },
        )
        self.assertTrue(
            all(
                1 <= review["answer_quality"][key] <= 5
                for key in (
                    "directness",
                    "insight",
                    "actionability",
                    "evidence_discipline",
                )
            )
        )
        self.assertEqual(
            review["final_answer_audit_coverage"],
            {"available": 0, "unavailable": 1},
        )
        self.assertIn(
            "final_answer_audit_unavailable",
            review["answer_quality"]["risk_markers"],
        )
        self.assertFalse(review["quality_scores_block_display"])

    def test_eval_review_tool_reads_run_matched_internal_final_llm_audit(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.review_analysis_contract_eval import review_artifact

        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            eval_dir = artifact_root / "phase7" / "eval"
            internal_dir = artifact_root / "phase-7" / "run-reviewed"
            eval_dir.mkdir(parents=True)
            internal_dir.mkdir(parents=True)
            internal_path = internal_dir / "answer_package.json"
            internal_path.write_text(
                json.dumps({
                    "run_id": "run-reviewed",
                    "final_answer": "内部完整业务回答。",
                    "quality_gate": {
                        "direct_answer": True,
                        "business_insight_present": True,
                        "followups_one_intent": False,
                        "has_verified_claims": True,
                        "verified_claim_preserved": True,
                        "repairable_warnings": ["weak_followup"],
                        "risk_flags": ["causal_wording_risk"],
                    },
                    "llm_calls": [{
                        "task": "final_answer_audit",
                        "structured_output": {
                            "display_status": "ready_with_warnings",
                            "repairable_warnings": ["weak_followup"],
                        },
                    }],
                }),
                encoding="utf-8",
            )
            case_path = eval_dir / "case.json"
            case_path.write_text(
                json.dumps({
                    "case_id": "fixed-review",
                    "turns": [{
                        "index": 1,
                        "run_id": "run-reviewed",
                        "artifact_path": str(internal_path),
                        "real_clickhouse_review": {
                            "runtime_correctness": {
                                "all_required_queries_complete": True,
                                "all_capabilities_bound": True,
                                "all_claims_traceable": True,
                            },
                            "issues": [],
                        },
                        "quality_review": {
                            "direct_answer": False,
                            "business_insight_present": False,
                            "followups_one_intent": False,
                            "has_verified_claims": False,
                            "verified_claim_preserved": False,
                            "quality_warnings": [],
                            "risk_markers": [],
                        },
                        "answer_package": {"final_answer": "客户端安全回答。"},
                    }],
                }),
                encoding="utf-8",
            )

            review = review_artifact(case_path)

        turn = review["turns"][0]
        self.assertEqual(turn["final_answer_audit_status"], "available")
        self.assertEqual(
            turn["answer_quality"],
            {
                "directness": 5,
                "insight": 5,
                "actionability": 2,
                "evidence_discipline": 5,
                "risk_markers": ["causal_wording_risk", "weak_followup"],
            },
        )

    def test_eval_review_tool_fails_closed_on_mismatched_internal_run(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.review_analysis_contract_eval import review_artifact

        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            eval_dir = artifact_root / "phase7" / "eval"
            internal_dir = artifact_root / "phase-7" / "run-other"
            eval_dir.mkdir(parents=True)
            internal_dir.mkdir(parents=True)
            internal_path = internal_dir / "answer_package.json"
            internal_path.write_text(
                json.dumps({
                    "run_id": "run-other",
                    "quality_gate": {"direct_answer": True},
                    "llm_calls": [{"task": "final_answer_audit"}],
                }),
                encoding="utf-8",
            )
            case_path = eval_dir / "case.json"
            case_path.write_text(
                json.dumps({
                    "case_id": "fixed-review",
                    "turns": [{
                        "index": 1,
                        "run_id": "run-expected",
                        "artifact_path": str(internal_path),
                        "real_clickhouse_review": {
                            "runtime_correctness": {
                                "all_required_queries_complete": True,
                                "all_capabilities_bound": True,
                                "all_claims_traceable": True,
                            },
                            "issues": [],
                        },
                    }],
                }),
                encoding="utf-8",
            )

            review = review_artifact(case_path)

        turn = review["turns"][0]
        self.assertEqual(turn["final_answer_audit_status"], "run_id_mismatch")
        self.assertEqual(turn["answer_quality"]["directness"], 1)
        self.assertIn(
            "final_answer_audit_unavailable",
            turn["answer_quality"]["risk_markers"],
        )

    def test_eval_review_quality_delta_requires_complete_matched_audits(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.review_analysis_contract_eval import review_artifact

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts" / "phase7"
            root.mkdir(parents=True)
            current = root / "current.json"
            baseline = root / "baseline.json"
            payload = {
                "case_id": "audit-gated-comparison",
                "turns": [{
                    "index": 1,
                    "run_id": "run-missing-audit",
                    "real_clickhouse_review": {"runtime_correctness": {}},
                }],
            }
            current.write_text(json.dumps(payload), encoding="utf-8")
            baseline.write_text(json.dumps(payload), encoding="utf-8")

            review = review_artifact(current, baseline=baseline)

        comparison = review["baseline_comparison"]["answer_quality_delta"]
        self.assertEqual(
            comparison,
            {
                "available": False,
                "reason": "complete_run_matched_final_audit_required",
                "delta": None,
            },
        )

    def test_strict_eval_treats_final_wording_anchor_as_warning(self):
        from tools.phase7.run_live_conversation_system_test import _strict_quality_failed

        turn = {
            "expectation_review": {
                "missing_required_capabilities": [],
                "missing_final_answer_text": ["近 7 日均值"],
                "claim_support_policy_passed": True,
            },
            "answer_package": {
                "quality_gate": {
                    "blocks_display": False,
                    "issues": ["missing_business_interpretation"],
                }
            },
        }

        self.assertFalse(_strict_quality_failed(turn))

    def test_expectation_review_keeps_wording_anchor_as_warning(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["近 7 日均值"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "相比最近一周平均水平，昨天的付费金额明显偏高。",
                                "claims": [
                                    {
                                        "text": "昨天的付费金额明显偏高。",
                                        "evidence_refs": ["evidence:baseline-1"],
                                        "context_manifest_ref": "context-1",
                                        "reuse_decisions": [
                                            {
                                                "decision": "reuse",
                                                "result_ref": "result:baseline-1",
                                                "reason": "baseline_window_available",
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "manifest_id": "context-1",
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "evidence:baseline-1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertEqual(review["missing_final_answer_text"], ["近 7 日均值"])
        self.assertTrue(review["claim_support_policy_passed"])
        self.assertTrue(review["passed"])

    def test_expectation_review_requires_claims_for_hard_boundary_only_case(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"hard_boundary_final_answer_contains": ["不能直接说"]}},
            {"intent": "challenge", "topic_relation": "inherit_current"},
            {
                "intent": "challenge",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "现在只能说不能直接说活动已经证明有效。",
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "manifest_id": "context-1",
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "evidence:baseline-1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertEqual(review["claim_evidence_review"]["claim_count"], 0)
        self.assertFalse(review["claim_support_policy_passed"])
        self.assertFalse(review["passed"])

    def test_live_harness_uses_fresh_thread_for_each_case_run(self):
        from tools.phase7.run_live_conversation_system_test import _case_thread_id

        first = _case_thread_id({"id": "q2_q1_wajespecial_long_followup"})
        second = _case_thread_id({"id": "q2_q1_wajespecial_long_followup"})

        self.assertTrue(first.startswith("live-q2_q1_wajespecial_long_followup-"))
        self.assertTrue(second.startswith("live-q2_q1_wajespecial_long_followup-"))
        self.assertNotEqual(first, second)

    def test_live_harness_separates_real_and_dry_run_artifacts(self):
        from tools.phase7.run_live_conversation_system_test import (
            _default_artifact_dir,
            _run_mode,
        )

        self.assertEqual(
            _default_artifact_dir(real_llm=False, real_clickhouse=False),
            Path("artifacts/phase7/live-conversation-dry-run"),
        )
        self.assertEqual(
            _default_artifact_dir(real_llm=True, real_clickhouse=True),
            Path("artifacts/phase7/live-conversation-real"),
        )
        self.assertEqual(_run_mode(real_llm=False, real_clickhouse=False), "dry_run")
        self.assertEqual(_run_mode(real_llm=True, real_clickhouse=True), "real_llm_real_clickhouse")


def _verified_delivery_package(
    *,
    run_id,
    causal_wording=False,
    paid_amount=10.0,
    baseline_amount=None,
    claim_text=None,
    claim_numbers=None,
    extra_rows=(),
    claim_dimensions=None,
    channel="A",
    claim_selector_mode="",
    accepted_assumptions=(),
):
    from bi_agent.runtime.analysis_contracts import AnalysisContract
    from bi_agent.runtime.claim_provenance import (
        build_trusted_claim_provenance_record,
    )

    resolved_windows = {
        "target_day": {
            "start_inclusive": "2026-06-02",
            "end_exclusive": "2026-06-03",
            "timezone": "Africa/Lagos",
        }
    }
    rows = [
        {
            "window_id": "target_day",
            "window_role": "target",
            "observation_key": "2026-06-02",
            "paid_amount": paid_amount,
            "amount": paid_amount,
            "channel": channel,
        }
    ]
    if baseline_amount is not None:
        resolved_windows["baseline_day"] = {
            "start_inclusive": "2026-06-01",
            "end_exclusive": "2026-06-02",
            "timezone": "Africa/Lagos",
        }
        rows.append(
            {
                "window_id": "baseline_day",
                "window_role": "baseline",
                "observation_key": "2026-06-01",
                "paid_amount": baseline_amount,
                "amount": baseline_amount,
                "channel": channel,
            }
        )
    rows.extend(dict(row) for row in extra_rows)
    _, context = verified_dimension_scan_asset(
        rows=tuple(rows),
        required_fields=("window_id", "amount", "channel"),
        resolved_windows=resolved_windows,
        analysis_contract_ref=f"analysis:{run_id}:1",
    )
    resolver = context["evidence_resolver"]
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    context["runtime_registry"] = registry
    binding = resolver.resolve_capability_binding(context["binding_manifest_ref"])
    query_record = resolver.resolve_query_execution(binding.result_refs[0])
    query_contract = query_record.contract
    analysis_contract = AnalysisContract(
        analysis_contract_id=f"analysis:{run_id}:1",
        contract_version="1",
        question_families=("segment_or_factor_attribution",),
        target_metric_refs=tuple(
            binding.contract_ref for binding in query_contract.metric_bindings
        ),
        claim_intents=("segment_contribution_or_mix_shift",),
        scope={"type": "full_sample", "requested_metric_ids": ["paid_amount"]},
        business_timezone="Africa/Lagos",
        as_of="2026-06-03T11:00:00+00:00",
        resolved_windows=query_contract.resolved_windows,
        metric_bindings=query_contract.metric_bindings,
        dimension_bindings=query_contract.dimension_bindings,
        dataset_requirements=("paid_order_success",),
        capability_requirements=("segment_contribution", "answer_verify"),
        permission_scope="analyst",
    )
    claim_text = claim_text or (
        f"渠道 A 导致目标期付费金额为 {paid_amount:g}。"
        if causal_wording
        else f"渠道 A 的目标期付费金额为 {paid_amount:g}。"
    )
    evidence_ref = "segment:authoritative"
    verified_numbers = (
        {"paid_amount": paid_amount}
        if claim_numbers is None
        else claim_numbers
    )
    evidence = {
        "evidence_ref": evidence_ref,
        "capability_id": binding.capability_id,
        "analysis_contract_ref": binding.analysis_contract_ref,
        "capability_contract_ref": binding.plan_payload["capability_contract_ref"],
        "query_contract_refs": tuple(
            dict.fromkeys(
                (*binding.query_contract_refs, *binding.validation_query_contract_refs)
            )
        ),
        "result_refs": tuple(
            dict.fromkeys((*binding.result_refs, *binding.validation_result_refs))
        ),
        "query_execution_record_refs": tuple(
            dict.fromkeys(
                (
                    *binding.query_execution_record_refs,
                    *binding.validation_query_execution_record_refs,
                )
            )
        ),
        "query_execution_record_digests": tuple(
            dict.fromkeys(
                (
                    *binding.query_execution_record_digests,
                    *binding.validation_query_execution_record_digests,
                )
            )
        ),
        "rows_metadata_record_refs": tuple(
            dict.fromkeys(
                (
                    *binding.rows_metadata_record_refs,
                    *binding.validation_rows_metadata_record_refs,
                )
            )
        ),
        "rows_metadata_record_digests": tuple(
            dict.fromkeys(
                (
                    *binding.rows_metadata_record_digests,
                    *binding.validation_rows_metadata_record_digests,
                )
            )
        ),
        "completeness_report_refs": tuple(
            dict.fromkeys(
                (
                    *binding.completeness_report_refs,
                    *binding.validation_completeness_report_refs,
                )
            )
        ),
        "completeness_record_refs": tuple(
            dict.fromkeys(
                (
                    *binding.completeness_record_refs,
                    *binding.validation_completeness_record_refs,
                )
            )
        ),
        "completeness_record_digests": tuple(
            dict.fromkeys(
                (
                    *binding.completeness_record_digests,
                    *binding.validation_completeness_record_digests,
                )
            )
        ),
        "source_snapshot_refs": tuple(
            dict.fromkeys(
                (*binding.source_snapshot_refs, *binding.validation_source_snapshot_refs)
            )
        ),
        "supported_evidence_types": binding.supported_evidence_types,
        "supported_claim_types": binding.supported_claim_types,
        "maximum_claim_strength": binding.maximum_claim_strength,
        "maximum_claim_strength_rank": binding.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": binding.claim_strength_taxonomy_version,
        "input_status": binding.status,
        "input_completeness_statuses": binding.input_completeness_statuses,
        "binding_manifest_ref": binding.record_ref,
        "binding_manifest_digest": binding.binding_digest,
        "evidence_type": "statistical_association",
        "strength": "medium",
        "wording_limit": "supported",
        "typed_payload": dict(verified_numbers),
        "limitations": (),
    }
    claim = {
        "text": claim_text,
        "claim_strength": "observed",
        "claim_type": "segment_contribution_or_mix_shift",
        "evidence_refs": (evidence_ref,),
        "numbers": dict(verified_numbers),
    }
    if claim_selector_mode:
        query_record = resolver.resolve_query_execution(binding.result_refs[0])

        def selector(*, role, window_id, observation_key):
            return {
                "query_contract_ref": query_record.contract.query_contract_id,
                "result_ref": binding.result_refs[0],
                "metric_id": "paid_amount",
                "window_role": role,
                "window_id": window_id,
                "observation_key": observation_key,
                "dimensions": {"channel": channel},
                "grain": list(query_record.contract.result_shape.grain),
            }

        if claim_selector_mode == "target":
            claim["fact_selectors"] = {
                "paid_amount": selector(
                    role="target",
                    window_id="target_day",
                    observation_key="2026-06-02",
                )
            }
        elif claim_selector_mode == "delta_pair":
            claim["fact_selectors"] = {
                "delta": {
                    "metric_id": "paid_amount",
                    "target": selector(
                        role="target",
                        window_id="target_day",
                        observation_key="2026-06-02",
                    ),
                    "baseline": selector(
                        role="baseline",
                        window_id="baseline_day",
                        observation_key="2026-06-01",
                    ),
                }
            }
        else:
            raise ValueError(f"unsupported claim_selector_mode: {claim_selector_mode}")
    if claim_dimensions:
        claim["dimensions"] = dict(claim_dimensions)
    package = build_answer_package(
        run_id=run_id,
        draft_claims=(claim,),
        evidence=(evidence,),
        evidence_resolver=resolver,
        rows_loader=context["rows_loader"],
        runtime_registry=registry,
        release_resolver=context["release_resolver"],
        checkpoint_events=(),
        proposed_graph=("segment_contribution",),
        accepted_graph=("segment_contribution", "answer_verify"),
        rejected_or_degraded_mutations=(),
        validator_results=(),
        sql_text="SELECT 1",
        sql_hash="sha256:test",
        artifact_audit={"artifact_ref": "artifact:test"},
        analysis_contract=analysis_contract.to_dict(),
        answer_text=claim_text,
        final_business_summary=claim_text,
        trusted_claim_provenance_record=build_trusted_claim_provenance_record(
            run_id=run_id,
            artifact_refs=("artifact:test",),
            memory_refs=("memory:test",),
            reuse_decisions=({"source_ref": "asset:test", "decision": "reuse"},),
        ),
        context_assumptions=accepted_assumptions,
        accepted_degradation_choice=(
            dict(accepted_assumptions[0]) if accepted_assumptions else {}
        ),
        compiler_runtime_plan=(
            {"graph_metadata": {"accepted_assumptions": list(accepted_assumptions)}}
            if accepted_assumptions
            else {}
        ),
    )
    return package, context, claim_text


def _fact_selector(
    package,
    context,
    *,
    window_role,
    window_id,
    observation_key,
    dimensions,
):
    evidence = package["sections"][1]["payload"]["evidence"][0]
    resolver = context["evidence_resolver"]
    binding = resolver.resolve_capability_binding(evidence["binding_manifest_ref"])
    result_ref = binding.result_refs[0]
    query_record = resolver.resolve_query_execution(result_ref)
    return {
        "query_contract_ref": query_record.contract.query_contract_id,
        "result_ref": result_ref,
        "metric_id": "paid_amount",
        "window_role": window_role,
        "window_id": window_id,
        "observation_key": observation_key,
        "dimensions": dict(dimensions),
        "grain": list(query_record.contract.result_shape.grain),
    }


def _resign_reported_verifier(package, context):
    summary = package["sections"][0]["payload"]
    evidence = package["sections"][1]["payload"]["evidence"]
    verifier = verify_answer_package(
        draft_claims=summary["claims"],
        evidence=evidence,
        visible_limitations=collect_visible_limitations(evidence),
        evidence_resolver=context["evidence_resolver"],
        rows_loader=context["rows_loader"],
        runtime_registry=context["runtime_registry"],
        release_resolver=context["release_resolver"],
        delivery_text={"answer_text": summary["claims"][0]["text"]},
    )
    package["admin_audit"]["verifier"] = verifier
    for section in package["sections"]:
        if section.get("section_id") == "admin_audit":
            section["payload"]["verifier"] = verifier


def _run_verified_package_through_core(
    package,
    context,
    *,
    thread_id,
    run_id,
    with_runtime_records=True,
):
    def workflow(request):
        records = None
        if with_runtime_records:
            from tests.phase7.test_analysis_runtime_persistence import (
                _authority_bundle,
            )

            records = _authority_bundle(
                run_id=request["run_id"],
                thread_id=request["thread_id"],
                topic_id=request["topic_id"],
            )
        return WorkflowRunResult(
            status="draft",
            run_id=request["run_id"],
            answer_package=package,
            checkpoint_events=(),
            analysis_runtime_records=records,
        )

    store = InMemoryConversationStore()
    store.create_thread(thread_id, owner_id="analyst-1")
    core = ConversationAgentCore(
        store,
        workflow_runner=workflow,
        evidence_resolver=context["evidence_resolver"],
        rows_loader=context["rows_loader"],
        evidence_writer=context["evidence_resolver"]._runtime_writer(),
        runtime_registry=context["runtime_registry"],
        release_resolver=context["release_resolver"],
    )
    return (
        core.run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message="Q2 比 Q1 付费金额为什么变了？",
        ),
        store,
    )


def _failed_llm_audit(response_suffix):
    output = {
        "question_family": "data_quality_or_evidence_review",
        "analysis_requirements": {"context_sources": ["gameplay"]},
    }
    return {
        "task": "business_intent",
        "provider": "contract-test-provider",
        "model": "contract-test-model",
        "prompt_version": "contract-test-v1",
        "response_id": f"response-{response_suffix}",
        "structured_output": output,
        "raw_response_content": json.dumps(output, ensure_ascii=False),
    }


def fake_workflow(request):
    manifest = request.get("context_manifest") or {}
    reuse_decisions = list(
        request.get("reuse_decisions")
        or [{"decision": "rerun", "result_ref": "", "reason": "test_default"}]
    )
    return WorkflowRunResult(
        status="draft",
        run_id=request["run_id"],
        answer_package={
            "run_id": request["run_id"],
            "status": "draft",
            "snapshot_id": "2026H1",
            "permission_scope": "analyst",
            "follow_up_context": "这轮回答后可以继续追问渠道、异常和口径变化。",
            "sections": [
                {
                    "id": "summary",
                    "visibility": "business_summary",
                    "payload": {
                        "answer_text": "",
                        "claims": [],
                    },
                },
                {
                    "id": "evidence",
                    "visibility": "aggregate_evidence",
                    "payload": {
                        "evidence": []
                    },
                }
            ],
            "admin_audit": {
                "verifier": {
                    "status": "passed",
                    "errors": [],
                    "warnings": [],
                    "accepted_claim_indexes": [],
                    "rejected_claim_indexes": [],
                }
            },
        },
        artifact_path="artifacts/phase-7/run-agent-core/answer_package.json",
        checkpoint_events=({"node": "persist_artifact", "status": "completed"},),
    )


def fake_failed_workflow(request):
    return WorkflowRunResult(
        status="failed",
        run_id=request["run_id"],
        failure_reason="synthetic_failure",
    )


class StrictThreadStore(InMemoryConversationStore):
    def upsert_run(self, run_id, *, thread_id, turn_id="", topic_id="", status, request=None):
        if thread_id not in self.threads:
            raise AssertionError("thread must exist before run insert")
        return super().upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            status=status,
            request=request,
        )


class JsonStrictStore(InMemoryConversationStore):
    def upsert_run(self, run_id, *, thread_id, turn_id="", topic_id="", status, request=None):
        json.dumps(request or {}, ensure_ascii=False, sort_keys=True)
        return super().upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            status=status,
            request=request,
        )


if __name__ == "__main__":
    unittest.main()
