import unittest
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
from io import StringIO
from unittest.mock import patch

from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    _manifest_with_current_run_evidence,
)
from bi_agent.conversation.models import ConversationRunRequest
from bi_agent.conversation.runtime import (
    ConversationOrchestrationError,
    ConversationRuntime,
)
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.langgraph_workflow import (
    WorkflowRunResult,
    _analysis_runtime_request,
)
from bi_agent.runtime.analysis_runtime import AnalysisRuntimeRequest
from bi_agent.runtime.answer_package import (
    AuthorityFact,
    _claim_authority_facts,
    _project_claim_from_authority,
    _render_authority_facts,
    build_answer_package,
    collect_visible_limitations,
    reverify_answer_package_for_delivery,
    verify_answer_package,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_digest, canonical_value
from bi_agent.runtime.claim_provenance import (
    build_context_manifest_record,
    build_verified_claim_record,
    validated_context_manifest_record,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.analysis_asset_vectors import verified_dimension_scan_asset


class _EmptyRuntimeEvidenceResolver:
    def __getattr__(self, name):
        if name.startswith("resolve_"):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


class _UnitRuntimeStore:
    analysis_runtime_records = {}
    runs = {}
    audit_events = ()

    def runtime_evidence_resolver(self):
        return _EmptyRuntimeEvidenceResolver()


def _workflow_clarification_material(
    *,
    question_family="revenue_health_review",
    target_metric="paid_amount",
    baselines=(),
    requested_components=(),
    requested_dimensions=(),
    context_sources=(),
    claim_intents=(),
    scope=None,
    diagnostic_tags=(),
):
    from bi_agent.conversation.clarification_authority import (
        _compiled_goal_material_projection,
    )

    goal_bindings = [{"goal_id": "explain_change", "role": "primary"}]
    explicit_focus = {
        "component_ids": list(requested_components),
        "dimension_ids": list(requested_dimensions),
        "context_source_ids": list(context_sources),
    }
    goal_material = _compiled_goal_material_projection(
        goal_bindings=goal_bindings,
        target_metric=target_metric,
        explicit_focus=explicit_focus,
    )
    return {
        "original_intent": {
            "question_family": question_family,
            "question_families": [question_family],
            "primary_question_family": question_family,
            "secondary_question_families": [],
            "target_metric": target_metric,
            "goal_bindings": goal_bindings,
            "explicit_focus": explicit_focus,
            "baseline_candidates": list(baselines),
            "context_sources": list(goal_material["context_sources"]),
            "claim_intents": list(goal_material["claim_types"]),
            "requested_dimensions": list(goal_material["dimension_ids"]),
            "requested_components": list(goal_material["component_ids"]),
            "scope": scope,
        },
        "material_slots": {
            "target_metrics": [target_metric],
            "component_ids": list(goal_material["component_ids"]),
            "dimension_ids": list(goal_material["dimension_ids"]),
            "baselines": list(baselines),
            "context_sources": list(goal_material["context_sources"]),
            "claim_types": list(goal_material["claim_types"]),
            "required_outcomes": list(goal_material["required_outcomes"]),
            "analysis_axis_ids": list(goal_material["analysis_axis_ids"]),
            "diagnostic_tags": list(diagnostic_tags),
            "scope": scope,
        },
    }


class AgentCoreBridgeTest(unittest.TestCase):
    def test_agent_core_enforces_personal_thread_ownership_before_workflow(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-owner-boundary", owner_id="user-owner")
        workflow_calls = []

        def workflow(request):
            workflow_calls.append(request)
            return fake_workflow(request)

        core = ConversationAgentCore(store, workflow_runner=workflow)

        with self.assertRaisesRegex(EvidenceIntegrityError, "^thread_owner_mismatch$"):
            core.run_message(
                thread_id="thread-owner-boundary",
                run_id="run-wrong-owner",
                user_id="user-other",
                user_message="Q2 比 Q1 付费金额为什么变了？",
            )

        self.assertEqual(workflow_calls, [])
        self.assertNotIn("run-wrong-owner", store.runs)

        result = core.run_message(
            thread_id="thread-owner-boundary",
            run_id="run-correct-owner",
            user_id="user-owner",
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(workflow_calls), 1)
        self.assertNotIn("user_id", workflow_calls[0])
        self.assertNotIn("user-owner", json.dumps(workflow_calls[0], ensure_ascii=False))
        self.assertTrue(
            any(event.get("actor_id") == "user-owner" for event in store.audit_events)
        )

    def _run_matched_runtime_authority_resolver(self, package):
        from bi_agent.runtime.analysis_contracts import (
            analysis_contract_from_dict,
            analysis_contract_signature,
        )

        run_id = package["run_id"]
        contract = deepcopy(package["admin_audit"]["analysis_contract"])
        signature = analysis_contract_signature(
            analysis_contract_from_dict(contract)
        )

        def resolve(requested_run_id):
            if requested_run_id != run_id:
                return None
            return {
                "run_id": run_id,
                "analysis_contract": {
                    **contract,
                    "contract_signature": signature,
                },
                "stored_contract_signature": signature,
            }

        return resolve

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

    def test_pre_window_aggregate_raw_package_identity_still_reverifies(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-pre-window-aggregate-package",
        )
        summary = package["sections"][0]["payload"]
        evidence = tuple(package["sections"][1]["payload"]["evidence"])
        evidence_by_ref = {
            item["evidence_ref"]: item for item in evidence
        }
        current_claim = summary["claims"][0]
        facts = _claim_authority_facts(
            current_claim,
            evidence_by_ref=evidence_by_ref,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        )
        raw_fact = next(
            fact
            for fact in facts["authority_facts"]
            if fact.metric_id == "paid_amount"
            and fact.window_id == "target_day"
        )
        legacy_fact_payload = {
            "query_contract_ref": raw_fact.query_contract_ref,
            "result_ref": raw_fact.result_ref,
            "metric_id": raw_fact.metric_id,
            "value": str(raw_fact.value),
            "window_id": raw_fact.window_id,
            "window_role": raw_fact.window_role,
            "observation_key": raw_fact.observation_key,
            "dimensions": raw_fact.dimensions,
            "grain": raw_fact.grain,
            "value_semantics": raw_fact.value_semantics,
            "display_format": raw_fact.display_format,
        }
        legacy_fact_ref = (
            "authority-fact:sha256:"
            f"{canonical_digest(legacy_fact_payload)}"
        )
        provenance_fields = {
            "claim_ref",
            "claim_digest",
            "run_id",
            "context_manifest_ref",
            "result_refs",
            "completeness_record_refs",
            "artifact_refs",
            "memory_refs",
            "reuse_decisions",
            "provenance_record_ref",
        }
        legacy_factual = {
            key: deepcopy(value)
            for key, value in current_claim.items()
            if key not in provenance_fields
        }
        legacy_factual["fact_refs"] = [legacy_fact_ref]
        legacy_factual["fact_selectors"]["paid_amount"] = {
            key: value
            for key, value in legacy_factual["fact_selectors"][
                "paid_amount"
            ].items()
            if key
            not in {
                "aggregation",
                "required_complete_days",
                "observation_keys",
            }
        }
        legacy_factual["target"] = {
            key: value
            for key, value in legacy_factual["target"].items()
            if key not in {"aggregation", "required_complete_days"}
        }
        provenance = package["admin_audit"][
            "trusted_claim_provenance_records"
        ][0]
        legacy_claim = build_verified_claim_record(
            legacy_factual,
            run_id=package["run_id"],
            context_manifest=package["admin_audit"]["context_manifest"],
            evidence_by_ref=evidence_by_ref,
            trusted_provenance=provenance,
        )
        summary["claims"][0] = legacy_claim

        delivered = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertNotIn(
            "aggregation",
            legacy_claim["fact_selectors"]["paid_amount"],
        )
        self.assertNotIn("aggregation", legacy_claim["target"])
        self.assertEqual(delivered["status"], "draft", delivered)

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
                "repair_path": "补齐背景证据后继续。",
            },
            context_manifest={
                "thread_id": "thread-zero-claim",
                "topic_id": "topic-zero-claim",
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
            records = _queryless_runtime_records_for_request(request)
            return _completed_runtime_workflow_result(
                request,
                answer_package=result.answer_package,
                records=records,
                artifact_path="",
                checkpoint_events=(
                    {"node": "persist_artifact", "status": "completed"},
                ),
                llm_calls=(_failed_llm_audit("runtime-persistence"),),
            )

        store = FailingStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-persistence-failure",
            run_id="run-persistence-failure",
            user_message="昨天付费金额为什么变化？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_store_commit_failed",
        )
        self.assertEqual(result["failure_stage"], "store_commit")
        self.assertEqual(result["failure_subreason"], "RuntimeError")
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
            if item["event_type"] == "analysis_runtime_store_commit_failed"
        )
        self.assertEqual(failure_audit["payload"]["reason"], "postgres unavailable")
        self.assertTrue(
            any(
                event["event_type"]
                == "workflow_failure_llm_call_recorded"
                for event in store.audit_events
            )
        )

    def test_post_publication_delivery_failures_are_typed_terminal_failures(self):
        from tempfile import TemporaryDirectory

        artifact_path = (
            Path(self.enterContext(TemporaryDirectory())) / "answer_package.json"
        )
        artifact_path.write_text("{}", encoding="utf-8")

        class FailingDeliveryStore(InMemoryConversationStore):
            def __init__(self, failing_writer):
                super().__init__()
                self.failing_writer = failing_writer
                self.runtime_published = False

            def save_analysis_runtime_records(self, **kwargs):
                result = super().save_analysis_runtime_records(**kwargs)
                self.runtime_published = True
                return result

            def record_context_manifest(self, manifest):
                if (
                    self.runtime_published
                    and self.failing_writer == "record_context_manifest"
                ):
                    raise RuntimeError("context manifest delivery unavailable")
                return super().record_context_manifest(manifest)

            def record_answer_package(self, run_id, package):
                if self.failing_writer == "record_answer_package":
                    raise RuntimeError("answer package delivery unavailable")
                return super().record_answer_package(run_id, package)

            def save_analysis_assets(self, thread_id, topic_id, assets):
                if self.failing_writer == "save_analysis_assets":
                    raise RuntimeError("analysis assets delivery unavailable")
                return super().save_analysis_assets(thread_id, topic_id, assets)

        def workflow(request):
            result = fake_workflow(request)
            records = _queryless_runtime_records_for_request(request)
            return _completed_runtime_workflow_result(
                request,
                answer_package=result.answer_package,
                records=records,
                artifact_path=str(artifact_path),
            )

        for writer in (
            "record_context_manifest",
            "record_answer_package",
            "save_analysis_assets",
        ):
            with self.subTest(writer=writer):
                run_id = f"run-delivery-failure-{writer}"
                store = FailingDeliveryStore(writer)

                result = ConversationAgentCore(
                    store,
                    workflow_runner=workflow,
                ).run_message(
                    thread_id=f"thread-delivery-failure-{writer}",
                    run_id=run_id,
                    user_message="当前付费金额的数据边界是什么？",
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(
                    result["failure_reason"],
                    "analysis_delivery_persistence_failed",
                )
                self.assertEqual(store.runs[run_id]["status"], "failed")
                self.assertEqual(
                    store.runs[run_id]["request"]["artifact_path"],
                    str(artifact_path),
                )
                self.assertIn(run_id, store.analysis_runtime_records)
                self.assertIn(
                    f"analysis:{run_id}:1",
                    store.analysis_runtime_authority["analysis_contract"],
                )
                failure = next(
                    event
                    for event in store.audit_events
                    if event["event_type"]
                    == "analysis_delivery_persistence_failed"
                )
                self.assertIn("delivery unavailable", failure["payload"]["reason"])

    def test_completed_run_publishes_material_authority_after_verified_runtime_persistence(self):
        from bi_agent.conversation.clarification_authority import (
            build_material_authority,
        )
        from tests.phase7.test_material_authority import (
            _runtime_material_for_contract,
        )

        run_id = "run-completed-material-authority"
        thread_id = "thread-completed-material-authority"
        package, context, _ = _verified_delivery_package(run_id=run_id)

        def workflow(request):
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
            )
            contract = records["analysis_contract"]
            original_intent = {
                "question_family": contract["question_families"][0],
                "question_families": list(contract["question_families"]),
                "primary_question_family": contract["question_families"][0],
                "secondary_question_families": list(
                    contract["question_families"][1:]
                ),
                "target_metric": "paid_amount",
                "requested_components": [],
                "requested_dimensions": [],
                "baseline_candidates": [],
                "context_sources": [],
                "claim_intents": list(contract["claim_intents"]),
                "scope": "full_sample",
            }
            material_slots = {
                "target_metrics": ["paid_amount"],
                "requested_components": [],
                "requested_dimensions": [],
                "baselines": [],
                "context_sources": [],
                "claim_intents": list(contract["claim_intents"]),
                "diagnostic_tags": [],
                "scope": "full_sample",
            }
            material_authority = build_material_authority(
                source_run_id=run_id,
                thread_id=thread_id,
                topic_id=request["topic_id"],
                original_intent=original_intent,
                material_slots=material_slots,
                runtime_material=_runtime_material_for_contract(contract),
            )
            from tests.phase7.artifact_test_support import (
                materialize_answer_package_artifact,
            )

            artifact_path, _ = materialize_answer_package_artifact(
                run_id=run_id,
                answer_package=package,
            )
            return WorkflowRunResult(
                status="draft",
                run_id=run_id,
                answer_package=package,
                analysis_runtime_records=records,
                completed_material_authority=material_authority,
                artifact_path=artifact_path,
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            evidence_writer=context["evidence_resolver"]._runtime_writer(),
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        ).run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        authority = store.resolve_completed_material_authority(
            source_run_id=run_id,
            thread_id=thread_id,
            topic_id=result["topic_id"],
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            authority["material_authority"],
            store.runs[run_id]["request"]["material_authority"],
        )
        self.assertEqual(
            sum(
                event["event_type"]
                == "completed_material_authority_recorded"
                for event in store.audit_events
            ),
            1,
        )

    def test_production_runtime_result_without_completed_material_authority_fails_closed(self):
        def workflow(request):
            result = fake_workflow(request)
            return WorkflowRunResult(
                status=result.status,
                run_id=result.run_id,
                answer_package=result.answer_package,
                analysis_runtime_result=object(),
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
        ).run_message(
            thread_id="thread-missing-completed-material",
            run_id="run-missing-completed-material",
            user_message="昨天付费金额为什么变化？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "material_authority_projection_failed",
        )
        self.assertEqual(
            result["failure_stage"],
            "material_authority_projection",
        )
        self.assertEqual(
            store.runs["run-missing-completed-material"]["status"],
            "failed",
        )
        self.assertFalse(
            any(
                event["event_type"]
                == "completed_material_authority_recorded"
                for event in store.audit_events
            )
        )

    def test_queryless_completed_material_authority_resolves_reviewed_target(self):
        def workflow(request):
            result = fake_workflow(request)
            records = _queryless_runtime_records_for_request(request)
            return _completed_runtime_workflow_result(
                request,
                answer_package=result.answer_package,
                records=records,
                artifact_path="",
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-queryless-completed-authority",
            run_id="run-queryless-completed-authority",
            user_message="当前付费金额的数据边界是什么？",
        )

        self.assertEqual(result["status"], "completed")
        authority = store.resolve_completed_material_authority(
            source_run_id=result["run_id"],
            thread_id="thread-queryless-completed-authority",
            topic_id=result["topic_id"],
        )
        self.assertEqual(
            authority["material_authority"]["intent_material"]["target_metrics"],
            ["paid_amount"],
        )

    def test_cross_owner_completed_authority_fails_before_any_publication(self):
        class PublicationTrackingStore(InMemoryConversationStore):
            def __init__(self):
                super().__init__()
                self.runtime_saves = 0

            def save_analysis_runtime_records(self, **kwargs):
                self.runtime_saves += 1
                return super().save_analysis_runtime_records(**kwargs)

        def workflow(request):
            result = fake_workflow(request)
            records = _queryless_runtime_records_for_request(request)
            completed = _completed_runtime_workflow_result(
                request,
                answer_package=result.answer_package,
                records=records,
                artifact_path="",
            )
            foreign_owner = {
                **request,
                "run_id": "run-foreign-owner",
                "thread_id": "thread-foreign-owner",
                "topic_id": "topic-foreign-owner",
            }
            return replace(
                completed,
                completed_material_authority=(
                    _completed_material_authority_for_records(
                        foreign_owner,
                        records,
                    )
                ),
            )

        store = PublicationTrackingStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-current-owner",
            run_id="run-current-owner",
            user_message="当前付费金额的数据边界是什么？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "completed_material_authority_preflight_failed",
        )
        self.assertEqual(
            result["failure_subreason"],
            "material_authority_owner_mismatch",
        )
        self.assertEqual(store.runtime_saves, 0)
        self.assertNotIn("run-current-owner", store.answer_packages)
        self.assertEqual(dict(store.analysis_assets), {})
        self.assertEqual(
            dict(store.analysis_runtime_authority).get("analysis_contract", {}),
            {},
        )

    def test_unknown_or_ambiguous_queryless_target_fails_before_runtime_publication(self):
        class PublicationTrackingStore(InMemoryConversationStore):
            def __init__(self):
                super().__init__()
                self.runtime_publication_attempts = 0

            def save_analysis_runtime_records(self, **kwargs):
                self.runtime_publication_attempts += 1
                return super().save_analysis_runtime_records(**kwargs)

        for target_ref in (
            "contracts/metrics/unknown.metric.yaml@0.1",
            "contracts/backlog/missing-contracts.yaml#component_contracts",
        ):
            with self.subTest(target_ref=target_ref):
                def workflow(request):
                    result = fake_workflow(request)
                    records = _queryless_runtime_records_for_request(
                        request,
                        target_ref=target_ref,
                    )
                    return _completed_runtime_workflow_result(
                        request,
                        answer_package=result.answer_package,
                        records=records,
                        artifact_path=result.artifact_path,
                    )

                store = PublicationTrackingStore()
                result = ConversationAgentCore(
                    store,
                    workflow_runner=workflow,
                ).run_message(
                    thread_id=f"thread-queryless-preflight-{store.runtime_publication_attempts}",
                    user_message="当前付费金额的数据边界是什么？",
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(
                    result["failure_reason"],
                    "completed_material_authority_preflight_failed",
                )
                self.assertEqual(
                    result["failure_subreason"],
                    "material_authority_contract_target_metrics_unresolvable",
                )
                self.assertEqual(
                    result["artifact_path"],
                    "artifacts/phase-7/run-agent-core/answer_package.json",
                )
                self.assertEqual(store.runtime_publication_attempts, 0)
                self.assertNotIn(result["run_id"], store.answer_packages)
                self.assertFalse(
                    any(
                        event["event_type"] == "analysis_runtime_partial_publication"
                        for event in store.audit_events
                    )
                )

    def test_completed_material_authority_anchor_failure_is_typed_finalization_failure(self):
        class FailingAuthorityStore(InMemoryConversationStore):
            def finalize_completed_material_authority(self, **_kwargs):
                raise EvidenceIntegrityError(
                    "completed_followup_authority_anchor_unavailable"
                )

        def workflow(request):
            result = fake_workflow(request)
            return WorkflowRunResult(
                status=result.status,
                run_id=result.run_id,
                answer_package=result.answer_package,
                artifact_path=result.artifact_path,
                completed_material_authority={"invalid": "store owns validation"},
            )

        store = FailingAuthorityStore()
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
        ).run_message(
            thread_id="thread-completed-anchor-failure",
            run_id="run-completed-anchor-failure",
            user_message="昨天付费金额为什么变化？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "completed_material_authority_finalization_failed",
        )
        self.assertEqual(
            result["failure_subreason"],
            "completed_followup_authority_anchor_unavailable",
        )
        self.assertEqual(
            result["artifact_path"],
            "artifacts/phase-7/run-agent-core/answer_package.json",
        )
        self.assertEqual(
            store.runs["run-completed-anchor-failure"]["status"],
            "failed",
        )
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"]
            == "completed_material_authority_finalization_failed"
        )
        self.assertEqual(
            failure["payload"]["failure_subreason"],
            "completed_followup_authority_anchor_unavailable",
        )
        self.assertFalse(
            any(
                event["event_type"] == "analysis_runtime_partial_publication"
                for event in store.audit_events
            )
        )

    def test_generic_completion_recovers_after_commit_then_error(self):
        class CommitThenErrorStore(InMemoryConversationStore):
            def __init__(self):
                super().__init__()
                self.recovery_attempts = 0

            def upsert_run(self, run_id, **kwargs):
                super().upsert_run(run_id, **kwargs)
                if kwargs["status"] == "completed":
                    raise RuntimeError("completion_acknowledgement_lost")

            def recover_after_write_failure(self):
                self.recovery_attempts += 1

            def get_run_state(self, run_id):
                return deepcopy(self.runs[run_id])

        run_id = "run-generic-completion-commit-then-error"
        thread_id = "thread-generic-completion-commit-then-error"
        store = CommitThenErrorStore()
        store.create_thread(thread_id, owner_id="analyst-1")

        result = ConversationAgentCore(
            store,
            workflow_runner=fake_workflow,
        ).run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message="昨天付费金额有什么变化？",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.runs[run_id]["status"], "completed")
        self.assertEqual(store.recovery_attempts, 1)
        self.assertFalse(
            any(
                event["event_type"]
                == "completed_material_authority_finalization_failed"
                for event in store.audit_events
            )
        )

    def test_terminal_completion_conflict_does_not_escape_or_downgrade(self):
        class MismatchedCommitThenErrorStore(InMemoryConversationStore):
            def upsert_run(self, run_id, **kwargs):
                super().upsert_run(run_id, **kwargs)
                if kwargs["status"] == "completed":
                    raise RuntimeError("completion_acknowledgement_lost")

            def recover_after_write_failure(self):
                return None

            def get_run_state(self, run_id):
                state = deepcopy(self.runs[run_id])
                state["request"] = {"question": "different persisted request"}
                return state

        run_id = "run-terminal-completion-conflict"
        thread_id = "thread-terminal-completion-conflict"
        store = MismatchedCommitThenErrorStore()
        store.create_thread(thread_id, owner_id="analyst-1")

        result = ConversationAgentCore(
            store,
            workflow_runner=fake_workflow,
        ).run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message="昨天付费金额有什么变化？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "completed_material_authority_finalization_failed",
        )
        self.assertEqual(store.runs[run_id]["status"], "completed")
        self.assertEqual(
            sum(
                event["event_type"]
                == "completed_material_authority_finalization_failed"
                for event in store.audit_events
            ),
            1,
        )

    def test_completed_authority_failure_leaves_zero_result_reuse_candidates(self):
        class FailingAuthorityStore(InMemoryConversationStore):
            def finalize_completed_material_authority(self, **_kwargs):
                raise EvidenceIntegrityError(
                    "completed_followup_authority_anchor_unavailable"
                )

        run_id = "run-anchor-failure-no-candidates"
        thread_id = "thread-anchor-failure-no-candidates"
        package, context, _ = _verified_delivery_package(run_id=run_id)

        def workflow(request):
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
                checkpoint_events=(),
            )

        store = FailingAuthorityStore()
        store.create_thread(thread_id, owner_id="analyst-1")
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            evidence_writer=context["evidence_resolver"]._runtime_writer(),
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        ).run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.runs[run_id]["status"], "failed")
        self.assertFalse(store.results_for_topic(result["topic_id"]))

    def test_result_candidate_publication_failure_preserves_completed_authority(self):
        class FailingCandidateStore(InMemoryConversationStore):
            def __init__(self):
                super().__init__()
                self.failure_boundary_events = []

            def add_result_ref(self, *_args, **_kwargs):
                self.failure_boundary_events.append("publication")
                raise EvidenceIntegrityError("result_reuse_index_unavailable")

            def recover_after_write_failure(self):
                self.failure_boundary_events.append("recovery")

            def add_audit_event(self, event_type, **kwargs):
                if event_type == "followup_index_publication_failed":
                    self.failure_boundary_events.append("audit")
                return super().add_audit_event(event_type, **kwargs)

        run_id = "run-candidate-publication-failure"
        thread_id = "thread-candidate-publication-failure"
        package, context, _ = _verified_delivery_package(run_id=run_id)

        def workflow(request):
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
                checkpoint_events=(),
            )

        store = FailingCandidateStore()
        store.create_thread(thread_id, owner_id="analyst-1")
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            evidence_writer=context["evidence_resolver"]._runtime_writer(),
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        ).run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.runs[run_id]["status"], "completed")
        self.assertEqual(
            store.failure_boundary_events,
            ["publication", "recovery", "audit"],
        )
        self.assertEqual(
            result["followup_index"],
            {
                "status": "failed",
                "failure_reason": "followup_index_publication_failed",
                "reason": "result_reuse_index_unavailable",
                "error_type": "EvidenceIntegrityError",
                "recovery_status": "recovered",
                "audit_status": "recorded",
            },
        )
        authority = store.resolve_completed_material_authority(
            source_run_id=run_id,
            thread_id=thread_id,
            topic_id=result["topic_id"],
        )
        self.assertEqual(authority["source_run_id"], run_id)
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"] == "followup_index_publication_failed"
        )
        self.assertEqual(
            failure["payload"],
            {
                "status": "failed",
                "failure_reason": "followup_index_publication_failed",
                "reason": "result_reuse_index_unavailable",
                "error_type": "EvidenceIntegrityError",
                "recovery_status": "recovered",
            },
        )

    def test_followup_index_recovery_and_audit_failures_do_not_escape_completed_run(self):
        class BoundaryFailureStore(InMemoryConversationStore):
            def __init__(self, failure_stage):
                super().__init__()
                self.failure_stage = failure_stage

            def add_result_ref(self, *_args, **_kwargs):
                raise EvidenceIntegrityError("result_reuse_index_unavailable")

            def recover_after_write_failure(self):
                if self.failure_stage == "recovery":
                    raise RuntimeError("write_recovery_unavailable")

            def add_audit_event(self, event_type, **kwargs):
                if (
                    event_type == "followup_index_publication_failed"
                    and self.failure_stage == "audit"
                ):
                    raise RuntimeError("followup_index_audit_unavailable")
                return super().add_audit_event(event_type, **kwargs)

        for failure_stage in ("recovery", "audit"):
            with self.subTest(failure_stage=failure_stage):
                run_id = f"run-followup-index-{failure_stage}-failure"
                thread_id = f"thread-followup-index-{failure_stage}-failure"
                package, context, _ = _verified_delivery_package(run_id=run_id)

                def workflow(request):
                    records = _verified_runtime_records_for_request(
                        request,
                        answer_package=package,
                        context=context,
                    )
                    return _completed_runtime_workflow_result(
                        request,
                        answer_package=package,
                        records=records,
                        checkpoint_events=(),
                    )

                store = BoundaryFailureStore(failure_stage)
                store.create_thread(thread_id, owner_id="analyst-1")
                result = ConversationAgentCore(
                    store,
                    workflow_runner=workflow,
                    evidence_resolver=context["evidence_resolver"],
                    rows_loader=context["rows_loader"],
                    evidence_writer=context[
                        "evidence_resolver"
                    ]._runtime_writer(),
                    runtime_registry=context["runtime_registry"],
                    release_resolver=context["release_resolver"],
                ).run_message(
                    thread_id=thread_id,
                    run_id=run_id,
                    user_message="Q2 比 Q1 付费金额为什么变了？",
                )

                self.assertEqual(result["status"], "completed")
                self.assertEqual(store.runs[run_id]["status"], "completed")
                self.assertEqual(result["followup_index"]["status"], "failed")
                self.assertEqual(
                    result["followup_index"]["failure_reason"],
                    "followup_index_publication_failed",
                )
                if failure_stage == "recovery":
                    self.assertEqual(
                        result["followup_index"]["recovery_status"],
                        "failed",
                    )
                    self.assertEqual(
                        result["followup_index"]["recovery_reason"],
                        "write_recovery_unavailable",
                    )
                    self.assertEqual(
                        result["followup_index"]["audit_status"],
                        "recorded",
                    )
                else:
                    self.assertEqual(
                        result["followup_index"]["recovery_status"],
                        "recovered",
                    )
                    self.assertEqual(
                        result["followup_index"]["audit_status"],
                        "failed",
                    )
                    self.assertEqual(
                        result["followup_index"]["audit_reason"],
                        "followup_index_audit_unavailable",
                    )

    def test_workflow_material_authority_build_failure_returns_typed_failed_result(self):
        from bi_agent.runtime import langgraph_workflow as workflow

        class CompletedGraph:
            def invoke(self, state, config):
                return {
                    **state,
                    "run_id": state["run_id"],
                    "workflow_status": "draft",
                    "workflow_failure_reason": "",
                    "checkpoint_events": [],
                    "llm_calls": [],
                    "execution_material": {"present": True},
                    "intent": {},
                    "analysis_route": {},
                    "answer_package": {},
                    "artifact_path": "",
                }

        with patch.object(
            workflow,
            "build_pattern_graph",
            return_value=CompletedGraph(),
        ), patch.object(
            workflow,
            "build_material_authority",
            side_effect=EvidenceIntegrityError("material_authority_shape_invalid"),
        ):
            result = workflow.run_pattern_workflow(
                {
                    "run_id": "run-material-build-failure",
                    "thread_id": "thread-material-build-failure",
                    "topic_id": "topic-material-build-failure",
                    "llm_client": object(),
                }
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.failure_reason,
            "completed_material_authority_build_failed:"
            "material_authority_shape_invalid",
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
            "analysis_runtime_bundle_validation_failed",
        )
        self.assertEqual(result["failure_stage"], "runtime_bundle_validation")
        self.assertNotIn("run-missing-runtime-bundle", store.answer_packages)
        self.assertNotIn(
            "run-missing-runtime-bundle",
            store.analysis_runtime_records,
        )
        self.assertFalse(store.analysis_assets)
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"]
            == "analysis_runtime_bundle_validation_failed"
        )
        self.assertEqual(
            failure["payload"]["reason"],
            "analysis_runtime_records_missing",
        )

    def test_persisted_claim_context_manifest_is_delivery_manifest(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-canonical-delivery-context",
        )
        captured = {}

        def workflow(request):
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
            )
            captured["records"] = records
            captured["request_manifest"] = request["context_manifest"]
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
                checkpoint_events=(),
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

    def test_records_only_claim_scoped_authority_atomically_reprojects_answer_package(self):
        from tempfile import TemporaryDirectory

        run_id = "run-claim-scoped-package-projection"
        scenario = _claim_scoped_package_projection_scenario(run_id=run_id)
        store = scenario["store"]
        thread_id = scenario["thread_id"]
        artifact_dir = Path(self.enterContext(TemporaryDirectory()))
        artifact_path = artifact_dir / "answer_package.json"

        def workflow(request):
            package, records, runtime_result, physical_decision = scenario["build"](
                request
            )
            records = scenario["analysis_runtime"].build_persistence_bundle(
                runtime_result,
                answer_package=package,
                request=request,
                artifact_path=str(artifact_path),
            )
            self.assertEqual(
                request["topic_id"],
                physical_decision["topic_id"],
            )
            artifact_path.write_text(
                json.dumps(package, ensure_ascii=False),
                encoding="utf-8",
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
                artifact_path=str(artifact_path),
                checkpoint_events=(),
            )

        core = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            evidence_resolver=scenario["evidence_resolver"],
            rows_loader=scenario["rows_loader"],
            runtime_registry=scenario["runtime_registry"],
            release_resolver=scenario["release_resolver"],
        )
        result = core.run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message="继续分别说明目标期总额和渠道 B 的独立历史窗口付费金额。",
        )

        self.assertEqual(result["status"], "completed", result)
        package = result["answer_package"]
        persisted = store.analysis_runtime_records[run_id]["payload"]
        persisted_claims = list(persisted["verified_claims"])
        persisted_provenance = list(persisted["trusted_provenance_records"])
        persisted_context = persisted["context_manifests"][0]
        physical_decisions = [
            decision
            for claim in persisted_claims
            for decision in claim.get("reuse_decisions") or ()
            if decision.get("schema_version")
            == "physical-query-reuse-decision.v1"
        ]
        reuse_decisions = []
        seen_decisions = set()
        for provenance in persisted_provenance:
            for decision in provenance.get("reuse_decisions") or ():
                identity = decision.get("decision_ref") or canonical_digest(decision)
                if identity in seen_decisions:
                    continue
                seen_decisions.add(identity)
                reuse_decisions.append(decision)

        self.assertEqual(len(persisted_claims), 2)
        self.assertEqual(len(physical_decisions), 1)
        self.assertEqual(
            [
                decision
                for decision in persisted_claims[1]["reuse_decisions"]
                if decision.get("schema_version")
                == "physical-query-reuse-decision.v1"
            ],
            [],
        )
        self.assertEqual(
            [
                decision.get("decision")
                for decision in persisted_claims[1]["reuse_decisions"]
            ],
            ["fresh"],
        )
        self.assertNotIn(
            physical_decisions[0]["result_ref"],
            persisted_claims[1]["result_refs"],
        )

        summary = next(
            section["payload"]
            for section in package["sections"]
            if section["section_id"] == "summary"
        )
        summary_by_ref = {
            claim["claim_ref"]: claim for claim in summary["claims"]
        }
        self.assertEqual(
            list(summary_by_ref),
            [claim["claim_ref"] for claim in persisted_claims],
        )
        for persisted_claim in persisted_claims:
            projected = summary_by_ref[persisted_claim["claim_ref"]]
            for field in (
                "context_manifest_ref",
                "result_refs",
                "completeness_record_refs",
                "artifact_refs",
                "memory_refs",
                "provenance_record_ref",
            ):
                self.assertEqual(projected[field], persisted_claim[field])
            self.assertEqual(
                projected["reuse_decisions"],
                [
                    {
                        key: decision[key]
                        for key in ("source_ref", "result_ref", "decision")
                        if key in decision
                    }
                    for decision in persisted_claim["reuse_decisions"]
                ],
            )

        admin = package["admin_audit"]
        self.assertEqual(package["verified_claims"], persisted_claims)
        self.assertEqual(
            package["available_evidence_brief"]["verified_claims"],
            persisted_claims,
        )
        self.assertEqual(package["context_manifest_ref"], persisted_context["manifest_id"])
        self.assertEqual(package["reuse_decisions"], reuse_decisions)
        self.assertEqual(admin["context_manifest"], persisted_context)
        self.assertEqual(admin["verified_claims"], persisted_claims)
        self.assertEqual(
            admin["trusted_claim_provenance_records"],
            persisted_provenance,
        )
        self.assertEqual(admin["reuse_decisions"], reuse_decisions)
        self.assertIn(admin["verifier"]["status"], {"passed", "passed_with_warnings"})
        self.assertEqual(admin["verifier"].get("errors") or [], [])
        self.assertEqual(
            next(
                section["payload"]
                for section in package["sections"]
                if section["section_id"] == "admin_audit"
            ),
            admin,
        )
        artifact_package = json.loads(
            artifact_path.read_text(encoding="utf-8")
        )
        artifact_summary = next(
            section["payload"]
            for section in artifact_package["sections"]
            if section["section_id"] == "summary"
        )
        self.assertEqual(artifact_summary["claims"], summary["claims"])
        for field in (
            "status",
            "final_answer",
            "verified_claims",
            "context_manifest_ref",
            "reuse_decisions",
        ):
            self.assertEqual(artifact_package[field], package[field])

        artifact_admin = artifact_package["admin_audit"]
        for field in (
            "context_manifest",
            "verified_claims",
            "trusted_claim_provenance_records",
            "reuse_decisions",
        ):
            self.assertEqual(artifact_admin[field], admin[field])
        for replay_field in (
            "analysis_contract",
            "query_contracts",
            "query_results",
            "completeness_reports",
            "llm_calls",
            "semantic_audit",
            "validator_results",
        ):
            self.assertIn(replay_field, artifact_admin)
            self.assertNotIn(replay_field, admin)
        self.assertEqual(
            next(
                section["payload"]
                for section in artifact_package["sections"]
                if section["section_id"] == "admin_audit"
            ),
            artifact_admin,
        )

    def test_completed_verified_runtime_indexes_claim_linked_ready_results(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature
        from bi_agent.conversation.models import (
            RESULT_REUSE_CANDIDATE_FIELDS,
            validate_result_reuse_candidate,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-result-candidate-publication",
        )

        result, store = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-result-candidate-publication",
            run_id="run-result-candidate-publication",
        )

        persisted = store.analysis_runtime_records[
            "run-result-candidate-publication"
        ]["payload"]
        expected_refs = tuple(
            dict.fromkeys(
                ref
                for claim in persisted["verified_claims"]
                for ref in claim["result_refs"]
            )
        )
        candidates = store.results_for_topic(result["topic_id"])

        self.assertEqual(
            tuple(candidate.result_ref for candidate in candidates),
            tuple(reversed(expected_refs)),
        )
        self.assertTrue(candidates)
        self.assertEqual(
            {candidate.snapshot_id for candidate in candidates},
            {"2026H1"},
        )
        self.assertEqual(
            {candidate.contract_version for candidate in candidates},
            {"contracts-v1"},
        )
        self.assertEqual(
            {candidate.semantic_scope for candidate in candidates},
            {
                "analysis-contract:sha256:"
                + analysis_contract_signature(persisted["analysis_contract"])
            },
        )
        for candidate in candidates:
            with self.subTest(result_ref=candidate.result_ref):
                self.assertEqual(
                    set(candidate.payload),
                    set(RESULT_REUSE_CANDIDATE_FIELDS),
                )
                self.assertEqual(
                    validate_result_reuse_candidate(candidate.payload),
                    candidate.payload,
                )
                self.assertEqual(
                    candidate.payload["source_run_id"],
                    "run-result-candidate-publication",
                )
                self.assertEqual(
                    len(candidate.payload["source_snapshot_refs"]),
                    len(candidate.payload["source_snapshot_record_refs"]),
                )
                authority = store.resolve_result_candidate_authority(
                    result_ref=candidate.result_ref,
                    topic_id=result["topic_id"],
                )
                self.assertEqual(authority["run_status"], "completed")

    def test_missing_claimed_artifact_fails_before_result_candidate_publication(self):
        missing_path = Path(
            "artifacts/phase7/test-runtime-audit/"
            "run-missing-claimed-artifact/answer_package.json"
        )
        missing_path.unlink(missing_ok=True)

        def workflow(request):
            result = fake_workflow(request)
            return _completed_runtime_workflow_result(
                request,
                answer_package=result.answer_package,
                records=_queryless_runtime_records_for_request(request),
                artifact_path=str(missing_path),
                checkpoint_events=(),
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-missing-claimed-artifact",
            run_id="run-missing-claimed-artifact",
            user_message="当前付费金额的数据边界是什么？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_artifact_sync_failed",
        )
        self.assertEqual(result["failure_stage"], "artifact_synchronization")
        self.assertEqual(result["artifact_path"], str(missing_path))
        self.assertEqual(
            result["failure_subreason"],
            "analysis_runtime_artifact_sync_failed",
        )
        self.assertFalse(store.results_for_topic(result["topic_id"]))
        self.assertNotIn("run-missing-claimed-artifact", store.answer_packages)
        self.assertNotIn(
            "run-missing-claimed-artifact",
            store.analysis_runtime_records,
        )
        self.assertFalse(
            any(store.analysis_runtime_authority.values()),
            store.analysis_runtime_authority,
        )
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"] == "analysis_runtime_artifact_sync_failed"
        )
        self.assertEqual(
            failure["payload"]["reason"],
            "analysis_runtime_artifact_sync_failed",
        )
        self.assertEqual(
            failure["payload"]["failure_subreason"],
            "analysis_runtime_artifact_sync_failed",
        )
        self.assertEqual(
            store.runs["run-missing-claimed-artifact"]["request"][
                "failure_subreason"
            ],
            "analysis_runtime_artifact_sync_failed",
        )

    def test_artifact_sync_failure_blocks_runtime_publication(self):
        from tempfile import TemporaryDirectory

        artifact_path = Path(self.enterContext(TemporaryDirectory())) / "answer_package.json"
        package = fake_workflow(
            {"run_id": "run-artifact-sync-failure"}
        ).answer_package
        artifact_path.write_text(
            json.dumps(package, ensure_ascii=False),
            encoding="utf-8",
        )

        def workflow(request):
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=_queryless_runtime_records_for_request(request),
                artifact_path=str(artifact_path),
                checkpoint_events=(),
            )

        with patch(
            "bi_agent.conversation.agent_core.synchronize_existing_artifact",
            side_effect=OSError("artifact storage unavailable"),
        ):
            store = InMemoryConversationStore()
            result = ConversationAgentCore(
                store,
                workflow_runner=workflow,
            ).run_message(
                thread_id="thread-artifact-sync-failure",
                run_id="run-artifact-sync-failure",
                user_message="当前付费金额的数据边界是什么？",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_artifact_sync_failed",
        )
        self.assertEqual(result["failure_stage"], "artifact_synchronization")
        self.assertEqual(result["artifact_path"], str(artifact_path))
        self.assertEqual(result["failure_subreason"], "OSError")
        self.assertNotIn("run-artifact-sync-failure", store.analysis_runtime_records)
        self.assertFalse(
            any(store.analysis_runtime_authority.values()),
            store.analysis_runtime_authority,
        )
        self.assertNotIn("run-artifact-sync-failure", store.answer_packages)
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"] == "analysis_runtime_artifact_sync_failed"
        )
        self.assertEqual(failure["payload"]["reason"], "artifact storage unavailable")
        self.assertEqual(failure["payload"]["failure_subreason"], "OSError")
        self.assertEqual(
            store.runs["run-artifact-sync-failure"]["request"][
                "failure_subreason"
            ],
            "OSError",
        )

    def test_verified_runtime_rejects_dangling_artifact_provenance(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-dangling-artifact-provenance",
        )

        result, store = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-dangling-artifact-provenance",
            run_id="run-dangling-artifact-provenance",
            artifact_path="",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_bundle_validation_failed",
        )
        self.assertEqual(result["failure_stage"], "runtime_bundle_validation")
        self.assertEqual(
            result["failure_subreason"],
            "runtime_persistence_answer_package_artifact_missing",
        )
        self.assertNotIn(
            "run-dangling-artifact-provenance",
            store.analysis_runtime_records,
        )
        self.assertNotIn(
            "run-dangling-artifact-provenance",
            store.answer_packages,
        )

    def test_final_artifact_sync_publishes_canonical_artifact_authority(self):
        from bi_agent.runtime.evidence_authority import canonical_digest
        from tempfile import TemporaryDirectory

        run_id = "run-artifact-authority"
        artifact_path = (
            Path(self.enterContext(TemporaryDirectory())) / "answer_package.json"
        )
        package = fake_workflow({"run_id": run_id}).answer_package
        artifact_path.write_text(json.dumps(package), encoding="utf-8")

        def workflow(request):
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=_queryless_runtime_records_for_request(request),
                artifact_path=str(artifact_path),
                checkpoint_events=(),
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-artifact-authority",
            run_id=run_id,
            user_message="当前付费金额的数据边界是什么？",
        )

        self.assertEqual(result["status"], "completed")
        persisted_package = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact_ref = f"answer-package:{run_id}"
        artifact = store.analysis_runtime_authority[
            "answer_package_artifact"
        ][artifact_ref]
        self.assertEqual(artifact["canonical_path"], str(artifact_path.resolve()))
        self.assertEqual(
            artifact["payload_digest"],
            canonical_digest(persisted_package),
        )
        self.assertEqual(
            store.analysis_runtime_records[run_id]["payload"][
                "answer_package_artifacts"
            ],
            [artifact],
        )

    def test_completed_core_preserves_full_authority_artifact_for_replay(self):
        from tempfile import TemporaryDirectory

        run_id = "run-full-authority-artifact"
        artifact_path = (
            Path(self.enterContext(TemporaryDirectory())) / "answer_package.json"
        )
        package = deepcopy(fake_workflow({"run_id": run_id}).answer_package)
        package["sections"].append(
            {
                "id": "admin_audit",
                "visibility": "admin_audit",
                "payload": deepcopy(package["admin_audit"]),
            }
        )
        raw_llm_call = {
            "task": "answer_synthesis",
            "raw_response_content": "provider raw response",
            "structured_output": {"final_business_summary": "数据暂不可用。"},
        }
        checkpoint = {"node": "answer_synthesis", "status": "completed"}
        analysis_contract = {
            "analysis_contract_id": f"analysis:{run_id}:1",
            "target_metric_refs": [
                "contracts/metrics/paid-amount.metric.yaml@0.1"
            ],
        }
        query_contract = {"query_contract_id": f"query:{run_id}:1"}
        query_result = {"result_ref": f"result:{run_id}:1", "row_count": 0}
        completeness_report = {
            "completeness_report_ref": f"completeness:{run_id}:1",
            "status": "empty",
        }
        final_audit = {"status": "ready_with_warnings"}
        package.update(
            {
                "llm_calls": [raw_llm_call],
                "checkpoint_events": [checkpoint],
                "analysis_contract": analysis_contract,
                "query_contracts": [query_contract],
                "query_results": [query_result],
                "completeness_reports": [completeness_report],
                "final_audit": final_audit,
            }
        )
        artifact_path.write_text(
            json.dumps(package, ensure_ascii=False),
            encoding="utf-8",
        )

        def workflow(request):
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=_queryless_runtime_records_for_request(request),
                artifact_path=str(artifact_path),
                checkpoint_events=(checkpoint,),
                llm_calls=(raw_llm_call,),
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-full-authority-artifact",
            run_id=run_id,
            user_message="当前付费金额的数据边界是什么？",
        )

        self.assertEqual(result["status"], "completed")
        delivered = result["answer_package"]
        self.assertEqual(delivered["llm_calls"], [])
        self.assertEqual(delivered["checkpoint_events"], [])
        self.assertNotIn("analysis_contract", delivered)
        self.assertNotIn("query_contracts", delivered)
        self.assertNotIn("query_results", delivered)
        self.assertNotIn("completeness_reports", delivered)

        persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["llm_calls"], [raw_llm_call])
        self.assertEqual(persisted["checkpoint_events"], [checkpoint])
        self.assertEqual(persisted["analysis_contract"], analysis_contract)
        self.assertEqual(persisted["query_contracts"], [query_contract])
        self.assertEqual(persisted["query_results"], [query_result])
        self.assertEqual(
            persisted["completeness_reports"],
            [completeness_report],
        )
        self.assertEqual(persisted["final_audit"], final_audit)
        persisted_verifier = persisted["admin_audit"]["verifier"]
        delivered_verifier = delivered["admin_audit"]["verifier"]
        for field in (
            "status",
            "errors",
            "warnings",
            "accepted_claim_indexes",
            "rejected_claim_indexes",
        ):
            self.assertEqual(persisted_verifier[field], delivered_verifier[field])
        self.assertIn("accepted_assumptions", persisted_verifier)
        self.assertNotIn("accepted_assumptions", delivered_verifier)

        artifact_ref = f"answer-package:{run_id}"
        authority_record = store.analysis_runtime_authority[
            "answer_package_artifact"
        ][artifact_ref]
        self.assertEqual(
            authority_record["payload_digest"],
            canonical_digest(persisted),
        )

    def test_analysis_asset_projection_failure_blocks_all_publication(self):
        from tempfile import TemporaryDirectory

        artifact_path = Path(self.enterContext(TemporaryDirectory())) / "answer_package.json"
        package = fake_workflow(
            {"run_id": "run-analysis-asset-projection-failure"}
        ).answer_package
        artifact_path.write_text(
            json.dumps(package, ensure_ascii=False),
            encoding="utf-8",
        )

        def workflow(request):
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=_queryless_runtime_records_for_request(request),
                artifact_path=str(artifact_path),
                checkpoint_events=(),
            )

        with patch(
            "bi_agent.conversation.agent_core.build_analysis_assets",
            side_effect=RuntimeError("analysis asset projection failed"),
        ):
            store = InMemoryConversationStore()
            result = ConversationAgentCore(
                store,
                workflow_runner=workflow,
            ).run_message(
                thread_id="thread-analysis-asset-projection-failure",
                run_id="run-analysis-asset-projection-failure",
                user_message="当前付费金额的数据边界是什么？",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_bundle_validation_failed",
        )
        self.assertEqual(result["failure_stage"], "runtime_bundle_validation")
        self.assertEqual(result["failure_subreason"], "RuntimeError")
        self.assertEqual(result["artifact_path"], str(artifact_path))
        self.assertNotIn(
            "run-analysis-asset-projection-failure",
            store.analysis_runtime_records,
        )
        self.assertFalse(
            any(store.analysis_runtime_authority.values()),
            store.analysis_runtime_authority,
        )
        self.assertNotIn(
            "run-analysis-asset-projection-failure",
            store.answer_packages,
        )
        self.assertFalse(store.analysis_assets)
        self.assertEqual(
            store.runs["run-analysis-asset-projection-failure"]["request"][
                "failure_subreason"
            ],
            "RuntimeError",
        )

    def test_claim_fact_mismatch_is_typed_and_blocks_runtime_publication(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-claim-fact-mismatch",
        )

        def workflow(request):
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=_authoritative_runtime_records_for_request(request),
                checkpoint_events=(),
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        ).run_message(
            thread_id="thread-claim-fact-mismatch",
            run_id="run-claim-fact-mismatch",
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_bundle_validation_failed",
        )
        self.assertEqual(result["failure_stage"], "runtime_bundle_validation")
        self.assertEqual(
            result["failure_subreason"],
            "persisted_answer_package_claim_facts_mismatch",
        )
        self.assertNotIn("run-claim-fact-mismatch", store.analysis_runtime_records)
        self.assertFalse(
            any(store.analysis_runtime_authority.values()),
            store.analysis_runtime_authority,
        )
        self.assertEqual(
            store.runs["run-claim-fact-mismatch"]["request"][
                "failure_subreason"
            ],
            "persisted_answer_package_claim_facts_mismatch",
        )
        failure = next(
            event
            for event in store.audit_events
            if event["event_type"] == "analysis_runtime_bundle_validation_failed"
        )
        self.assertEqual(
            failure["payload"]["failure_subreason"],
            "persisted_answer_package_claim_facts_mismatch",
        )

    def test_next_agent_core_turn_receives_all_published_candidates(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-candidate-source",
        )
        captured = {}

        def source_workflow(request):
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
            )

        store = InMemoryConversationStore()
        core = ConversationAgentCore(
            store,
            workflow_runner=source_workflow,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            evidence_writer=context["evidence_resolver"]._runtime_writer(),
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        )
        first = core.run_message(
            thread_id="thread-candidate-handoff",
            run_id="run-candidate-source",
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        def follow_up_workflow(request):
            captured["request"] = request
            return WorkflowRunResult(
                status="failed",
                run_id=request["run_id"],
                failure_reason="capture_only",
            )

        core.workflow_runner = follow_up_workflow
        core.run_message(
            thread_id="thread-candidate-handoff",
            run_id="run-candidate-consumer",
            user_message="继续看刚才的渠道贡献。",
        )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(store.runs["run-candidate-source"]["status"], "completed")
        self.assertEqual(
            [
                candidate["result_ref"]
                for candidate in captured["request"]["reuse_candidates"]
            ],
            [
                candidate.result_ref
                for candidate in store.results_for_topic(first["topic_id"])
            ],
        )

    def test_completed_runtime_does_not_index_unverified_results(self):
        from tests.phase7.test_analysis_runtime_persistence import (
            _without_claim_authority,
        )

        def workflow(request):
            package = fake_workflow(request).answer_package
            records = _without_claim_authority(
                _authoritative_runtime_records_for_request(request)
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
            thread_id="thread-unverified-result-candidate",
            run_id="run-unverified-result-candidate",
            user_message="昨天付费金额为什么变化？",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.results_for_topic(result["topic_id"]), ())

    def test_incomplete_claim_runtime_fails_closed_without_indexing(self):
        package, context, _ = _verified_delivery_package(
            run_id="run-incomplete-result-candidate",
        )

        def workflow(request):
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
            )
            records["completeness_records"] = tuple(
                replace(
                    record,
                    report_payload={
                        **record.report_payload,
                        "completeness_status": "partial",
                        "analysis_readiness": "degraded",
                    },
                )
                for record in records["completeness_records"]
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
            )

        store = ContractAuthorityOnlyStore()
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
            thread_id="thread-incomplete-result-candidate",
            run_id="run-incomplete-result-candidate",
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure_reason"],
            "analysis_runtime_bundle_validation_failed",
        )
        self.assertEqual(result["failure_stage"], "runtime_bundle_validation")
        self.assertEqual(store.results_for_topic(result["topic_id"]), ())

    def test_failed_query_or_unready_binding_blocks_runtime_publication(self):
        for boundary in ("query_failed", "binding_not_ready"):
            with self.subTest(boundary=boundary):
                package, context, _ = _verified_delivery_package(
                    run_id=f"run-candidate-{boundary}",
                )

                def workflow(request, boundary=boundary):
                    records = _verified_runtime_records_for_request(
                        request,
                        answer_package=package,
                        context=context,
                    )
                    if boundary == "query_failed":
                        records["query_execution_records"] = tuple(
                            replace(record, execution_status="failed")
                            for record in records["query_execution_records"]
                        )
                    else:
                        records["capability_binding_records"] = tuple(
                            replace(record, status="waiting_for_evidence")
                            for record in records["capability_binding_records"]
                        )
                    return _completed_runtime_workflow_result(
                        request,
                        answer_package=package,
                        records=records,
                    )

                store = ContractAuthorityOnlyStore()
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
                    thread_id=f"thread-candidate-{boundary}",
                    run_id=f"run-candidate-{boundary}",
                    user_message="Q2 比 Q1 付费金额为什么变了？",
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(
                    result["failure_reason"],
                    "analysis_runtime_bundle_validation_failed",
                )
                self.assertEqual(
                    result["failure_stage"],
                    "runtime_bundle_validation",
                )
                self.assertEqual(store.results_for_topic(result["topic_id"]), ())

    def test_final_persisted_answer_package_replaces_existing_artifact(self):
        from tempfile import TemporaryDirectory

        package, context, _ = _verified_delivery_package(
            run_id="run-final-artifact-sync",
        )
        artifact_dir = Path(self.enterContext(TemporaryDirectory()))
        artifact_path = artifact_dir / "answer_package.json"
        artifact_path.write_text(
            json.dumps(package, ensure_ascii=False),
            encoding="utf-8",
        )

        def workflow(request):
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
                artifact_path=str(artifact_path),
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
                artifact_path=str(artifact_path),
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
            thread_id="thread-final-artifact-sync",
            run_id="run-final-artifact-sync",
            user_message="Q2 比 Q1 付费金额为什么变了？",
        )

        persisted_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted_artifact["verified_claims"],
            result["answer_package"]["verified_claims"],
        )
        self.assertEqual(
            persisted_artifact["admin_audit"]["analysis_runtime_persistence"],
            result["answer_package"]["admin_audit"][
                "analysis_runtime_persistence"
            ],
        )
        self.assertEqual(
            list(artifact_dir.glob(".*.tmp")),
            [],
        )

    def test_waiting_query_gap_persists_partial_runtime_closure_and_internal_audit(self):
        from bi_agent.runtime.analysis_runtime import AnalysisRuntime
        from tests.phase7.test_analysis_runtime_persistence import (
            _waiting_runtime_records_with_unbound_results,
        )

        runtime_result, _, bound_result_refs, all_result_refs = (
            _waiting_runtime_records_with_unbound_results()
        )

        def workflow(request):
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["segment_contribution"],
                    "analysis_contract": runtime_result.persistence_records[
                        "analysis_contract"
                    ],
                    "analysis_route": {"requested_nodes": ["segment_contribution"]},
                    **_workflow_clarification_material(),
                    "clarification": {
                        "questions": [{
                            "question": "部分结果尚未形成能力绑定，怎么继续？",
                            "options": [
                                "等待完整绑定后继续",
                                "tell the agent to do differently",
                            ],
                        }],
                        "recommended_assumption": {
                            "option": "等待完整绑定后继续"
                        },
                    },
                },
                analysis_runtime_records=runtime_result.persistence_records,
                analysis_runtime_result=runtime_result,
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            analysis_runtime=object.__new__(AnalysisRuntime),
        ).run_message(
            thread_id="thread-waiting-partial-runtime",
            run_id="run-task9",
            user_message="昨天付费金额的渠道贡献为什么变化？",
        )

        self.assertEqual(result["status"], "waiting_for_clarification")
        persisted = store.analysis_runtime_records["run-task9"]["payload"]
        self.assertEqual(
            {item["result_ref"] for item in persisted["query_execution_records"]},
            bound_result_refs,
        )
        audit = next(
            event
            for event in store.audit_events
            if event["event_type"] == "analysis_runtime_partial_publication"
        )
        self.assertEqual(
            set(audit["payload"]["omitted_result_refs"]),
            all_result_refs - bound_result_refs,
        )
        self.assertEqual(
            audit["payload"]["omitted_result_count"],
            len(all_result_refs - bound_result_refs),
        )
        self.assertEqual(
            audit["payload"]["owner"],
            "analysis_runtime_persistence_owner",
        )
        self.assertNotIn("partial_publication", result)
        self.assertNotIn("omitted_result_refs", str(result))


    def test_query_gap_waiting_persists_zero_claim_runtime_bundle_before_return(self):
        class CapturingStore(InMemoryConversationStore):
            def save_analysis_runtime_records(self, **kwargs):
                self.saved_runtime_bundle = kwargs
                return "inserted"

        def workflow(request):
            records = _queryless_runtime_records_for_request(request)
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["event_evidence"],
                    "analysis_contract": records["analysis_contract"],
                    "analysis_route": {"requested_nodes": ["event_evidence"]},
                    **_workflow_clarification_material(
                        context_sources=("external_event",),
                    ),
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
            "analysis:run-waiting-runtime-persistence:1",
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
                    **_workflow_clarification_material(
                        context_sources=("external_event",),
                    ),
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
            "analysis_runtime_bundle_validation_failed",
        )
        self.assertEqual(result["failure_stage"], "runtime_bundle_validation")
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

    def test_query_gap_user_redirect_attempt_opens_free_input_without_rerunning_workflow(self):
        calls = []
        resolution_id = "resolution-user-redirect-1"
        attempt_run_id = "run-user-redirect-attempt-1"
        selected_choice = {
            "choice_id": "user_redirect",
            "action_kind": "user_redirect",
            "business_label": "tell the agent to do differently",
            "affected_capabilities": [],
        }

        def workflow(request):
            calls.append(dict(request))
            if len(calls) > 1:
                raise AssertionError("user redirect must not rerun analysis")
            waiting_records = _queryless_runtime_records_for_request(request)
            return WorkflowRunResult(
                status="waiting_for_clarification",
                run_id=request["run_id"],
                answer_package={
                    "status": "waiting_for_clarification",
                    "accepted_graph": ["event_evidence"],
                    "analysis_contract": waiting_records["analysis_contract"],
                    "analysis_route": {"requested_nodes": ["event_evidence"]},
                    **_workflow_clarification_material(
                        context_sources=("external_event",),
                    ),
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
                analysis_runtime_records=waiting_records,
            )

        store = ContractAuthorityOnlyStore()
        core = ConversationAgentCore(store, workflow_runner=workflow)
        first = core.run_message(
            thread_id="thread-user-redirect",
            run_id="run-user-redirect-original",
            user_message="活动是否影响昨天？",
        )
        self.assertEqual(first["status"], "waiting_for_clarification", first)
        store.resolve_clarification_attempt_authority = lambda **values: {
            "resolution_id": resolution_id,
            "source_run_id": values["source_run_id"],
            "attempt_run_id": values["attempt_run_id"],
            "previous_attempt_run_id": None,
            "attempt_number": 1,
            "thread_id": values["thread_id"],
            "topic_id": first["topic_id"],
            "owner_id": store.get_thread(values["thread_id"]).owner_id,
            "answer": values["answer"],
            "selected_option_id": values["selected_option_id"],
            "source": values["source"],
            "retry_attempt": False,
            "accepted_choice": deepcopy(selected_choice),
            "material_patch": {},
        }
        redirected = core.run_message(
            thread_id="thread-user-redirect",
            run_id=attempt_run_id,
            user_message="tell the agent to do differently",
            clarification={
                "sourceRunId": "run-user-redirect-original",
                "resolutionId": resolution_id,
                "attemptRunId": attempt_run_id,
                "answer": "tell the agent to do differently",
                "selectedOptionId": selected_choice["choice_id"],
                "source": "user",
                "retryAttempt": False,
            },
        )

        self.assertEqual(redirected["status"], "waiting_for_clarification")
        self.assertTrue(redirected["user_redirect"])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(store.get_open_clarification("thread-user-redirect"))

    def test_general_clarification_attempt_keeps_original_intent_and_material_choice(self):
        captured = []
        resolution_id = "resolution-general-1"
        attempt_run_id = "run-general-attempt-1"
        selected_choice = {
            "choice_id": "material-scope-full-sample",
            "action_kind": "bind_material_choice",
            "business_label": "保留全样本范围继续。",
            "material_patch": {"scope": "full_sample"},
            "affected_material_slots": ["scope"],
        }

        def workflow(request):
            captured.append(dict(request))
            if len(captured) == 1:
                waiting_records = _queryless_runtime_records_for_request(request)
                return WorkflowRunResult(
                    status="waiting_for_clarification",
                    run_id=request["run_id"],
                    answer_package={
                        "status": "waiting_for_clarification",
                        "accepted_graph": [],
                        "analysis_contract": waiting_records["analysis_contract"],
                        "analysis_route": {
                            "requested_nodes": [],
                            "analysis_requirements": {
                                "target_metrics": ["paid_amount"],
                                "baselines": ["previous_day"],
                            },
                        },
                        **_workflow_clarification_material(
                            question_family="revenue_health_review",
                            target_metric="paid_amount",
                            baselines=("previous_day",),
                        ),
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
                    analysis_runtime_records=waiting_records,
                )
            return fake_workflow(request)

        store = ContractAuthorityOnlyStore()
        core = ConversationAgentCore(store, workflow_runner=workflow)
        first = core.run_message(
            thread_id="thread-general-clarification",
            run_id="run-general-original",
            user_message="比较昨天付费金额并说明范围。",
            analysis_context={"as_of": "2026-06-03T12:00:00+01:00"},
        )
        self.assertEqual(first["status"], "waiting_for_clarification", first)
        source_envelope = store.runs["run-general-original"]["request"][
            "clarification_source_envelope"
        ]
        store.resolve_clarification_attempt_authority = lambda **values: {
            "resolution_id": resolution_id,
            "source_run_id": values["source_run_id"],
            "attempt_run_id": values["attempt_run_id"],
            "previous_attempt_run_id": None,
            "attempt_number": 1,
            "thread_id": values["thread_id"],
            "topic_id": first["topic_id"],
            "owner_id": store.get_thread(values["thread_id"]).owner_id,
            "answer": values["answer"],
            "selected_option_id": values["selected_option_id"],
            "source": values["source"],
            "retry_attempt": False,
            "accepted_choice": deepcopy(selected_choice),
            "material_patch": deepcopy(selected_choice["material_patch"]),
        }
        attempt = core.run_message(
            thread_id="thread-general-clarification",
            run_id=attempt_run_id,
            user_message="保留全样本范围继续。",
            clarification={
                "sourceRunId": "run-general-original",
                "resolutionId": resolution_id,
                "attemptRunId": attempt_run_id,
                "answer": "保留全样本范围继续。",
                "selectedOptionId": selected_choice["choice_id"],
                "source": "user",
                "retryAttempt": False,
            },
            analysis_context={"as_of": "2026-07-13T12:00:00+01:00"},
        )

        self.assertEqual(first["status"], "waiting_for_clarification")
        self.assertEqual(attempt["topic_id"], first["topic_id"])
        self.assertEqual(
            source_envelope["question"],
            "比较昨天付费金额并说明范围。",
        )
        self.assertEqual(
            source_envelope["analysis_context"],
            {"as_of": "2026-06-03T12:00:00+01:00"},
        )
        self.assertEqual(
            source_envelope["source_material"]["original_intent"][
                "target_metric"
            ],
            "paid_amount",
        )
        self.assertEqual(
            source_envelope["source_material"]["material_slots"]["baselines"],
            ["previous_day"],
        )
        attempt_context = captured[1]["clarification_attempt_context"]
        self.assertEqual(attempt_context["source_run_id"], "run-general-original")
        self.assertEqual(
            attempt_context["original_intent"]["target_metric"],
            "paid_amount",
        )
        self.assertEqual(
            attempt_context["material_slots"]["baselines"],
            ["previous_day"],
        )
        self.assertEqual(attempt_context["accepted_choice"], selected_choice)
        self.assertEqual(
            attempt_context["selected_material_action"],
            selected_choice,
        )
        self.assertEqual(captured[1]["question"], "比较昨天付费金额并说明范围。")
        self.assertEqual(
            captured[1]["analysis_context"],
            {"as_of": "2026-07-13T12:00:00+01:00"},
        )

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
            ),
            runtime_registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

        self.assertEqual(
            rendered,
            ("2026-06-02的支付成功率为12.3%。",),
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

    def test_verified_business_narrative_survives_core_and_artifact_sync_verbatim(self):
        narrative = (
            "我对问题的理解是：核对渠道A的目标期付费金额。\n"
            "分析脉络：先确认数据权威，再核对目标窗口。\n"
            "关键发现：渠道A的目标期付费金额为10。\n"
            "最终结论：当前可以发布这一已观测结果。\n"
            "需要注意：该结论只覆盖当前目标窗口。"
        )
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-bound-business-narrative",
            claim_selector_mode="target",
            final_business_summary=narrative,
            narrative_statement_bindings=(
                {
                    "excerpt": "关键发现：渠道A的目标期付费金额为10",
                    "statement_class": "verified_claim",
                    "authority_keys": ["结论1"],
                },
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "answer-package.json"
            artifact_path.write_text(
                json.dumps(package, ensure_ascii=False),
                encoding="utf-8",
            )
            result, store = _run_verified_package_through_core(
                package,
                context,
                thread_id="thread-agent-core-bound-business-narrative",
                run_id="run-agent-core-bound-business-narrative",
                artifact_path=str(artifact_path),
            )

            self.assertEqual(result.get("status"), "completed", result)
            delivered = result["answer_package"]
            persisted = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(delivered["final_answer"], narrative)
        self.assertEqual(
            delivered["sections"][0]["payload"]["answer_text"],
            narrative,
        )
        self.assertEqual(
            delivered["sections"][0]["payload"]["final_business_summary"],
            narrative,
        )
        self.assertEqual(
            store.answer_packages[
                "run-agent-core-bound-business-narrative"
            ]["final_answer"],
            narrative,
        )
        self.assertEqual(persisted["final_answer"], narrative)
        self.assertEqual(
            persisted["sections"][0]["payload"]["answer_text"],
            narrative,
        )
        self.assertEqual(
            persisted["sections"][0]["payload"]["final_business_summary"],
            narrative,
        )
        self.assertNotEqual(
            narrative,
            delivered["sections"][0]["payload"]["claims"][0]["text"],
        )
        self.assertEqual(
            delivered["sections"][0]["payload"]["claims"][0]["numbers"],
            {"paid_amount": "10.0"},
        )

    def test_unsupported_auxiliary_narrative_is_pruned_without_erasing_verified_result(self):
        narrative = (
            "我对问题的理解是：核对渠道A的目标期付费金额。\n"
            "分析脉络：先确认数据权威，再核对目标窗口。\n"
            "关键发现：渠道A的目标期付费金额为10。\n"
            "最终结论：促销活动导致付费金额增加9,999。\n"
            "需要注意：该结论只覆盖当前目标窗口。"
        )
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-unbound-material-narrative",
            claim_selector_mode="target",
            final_business_summary=narrative,
            narrative_statement_bindings=(
                {
                    "excerpt": "关键发现：渠道A的目标期付费金额为10",
                    "statement_class": "verified_claim",
                    "authority_keys": ["结论1"],
                },
                {
                    "excerpt": "最终结论：促销活动导致付费金额增加9,999",
                    "statement_class": "verified_claim",
                    "authority_keys": ["结论1"],
                },
            ),
        )

        retained = "关键发现：渠道A的目标期付费金额为10"
        self.assertEqual(package["status"], "draft")
        self.assertEqual(package["final_answer"], retained)
        self.assertEqual(
            package["admin_audit"]["verifier"]["status"],
            "passed",
        )
        self.assertEqual(
            package["admin_audit"]["narrative_publication"]["status"],
            "passed",
        )
        repair = package["admin_audit"]["narrative_publication_repair"]
        self.assertEqual(repair["status"], "invalid_statements_removed")
        self.assertIn(1, repair["rejected_statement_indexes"])
        self.assertNotIn("促销活动", package["final_answer"])

        delivered = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=context["evidence_resolver"],
            rows_loader=context["rows_loader"],
            runtime_registry=context["runtime_registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(delivered["status"], "draft")
        self.assertEqual(delivered["final_answer"], retained)
        self.assertEqual(len(delivered["sections"][0]["payload"]["claims"]), 1)

    def test_verified_narrative_delivery_reverify_is_idempotent(self):
        narrative = "关键发现：渠道A的目标期付费金额为10"
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-narrative-reverify-idempotent",
            claim_selector_mode="target",
            final_business_summary=narrative,
            narrative_statement_bindings=(
                {
                    "excerpt": narrative,
                    "statement_class": "verified_claim",
                    "authority_keys": ["结论1"],
                },
            ),
        )

        def reverify(candidate):
            return reverify_answer_package_for_delivery(
                candidate,
                evidence_resolver=context["evidence_resolver"],
                rows_loader=context["rows_loader"],
                runtime_registry=context["runtime_registry"],
                release_resolver=context["release_resolver"],
            )

        delivered = reverify(package)
        delivered_again = reverify(delivered)

        self.assertEqual(delivered_again["status"], "draft")
        self.assertEqual(delivered_again["final_answer"], narrative)
        self.assertEqual(delivered_again, delivered)

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
                    "window_id": "previous_day",
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
                    "window_id": "previous_day",
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
                    "window_id": "previous_day",
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
                    "window_id": "previous_day",
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
            ("null", None, "null", "Blank"),
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
        self.assertEqual(
            selected_delivery["sections"][0]["payload"]["claims"][0][
                "dimensions"
            ],
            {"channel": "A"},
        )

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
        self.assertEqual(projected["comparison_direction"], "positive")
        self.assertEqual(projected["baseline"]["window_id"], "previous_day")

    def test_passed_comparison_uses_business_text_without_metric_or_window_ids(self):
        authority_facts = tuple(
            AuthorityFact.create(
                query_contract_ref="query:business-text",
                result_ref="result:business-text",
                metric_id="paid_amount",
                value=value,
                window_id=window_id,
                window_role=role,
                observation_key=observation_key,
                dimensions=(),
                grain=("window_id", "observation_key"),
                value_semantics="raw_scalar",
                display_format="decimal",
            )
            for role, window_id, observation_key, value in (
                ("target", "target_day", "2026-06-02", Decimal("20")),
                ("baseline", "previous_day", "2026-06-01", Decimal("10")),
            )
        )
        projected = _project_claim_from_authority(
            {
                "text": "目标期 paid_amount 为 20，高于基线的 10。",
                "claim_type": "comparative_change",
                "claim_strength": "observed",
                "evidence_refs": ("compare:business-text",),
                "numbers": {
                    "target_paid_amount": 20.0,
                    "baseline_paid_amount": 10.0,
                    "delta": 10.0,
                },
            },
            {
                "metric_ids": ("paid_amount",),
                "authority_facts": authority_facts,
                "authority_context_facts": (),
                "grains": (("window_id", "observation_key"),),
                "target_windows": (
                    {
                        "window_id": "target_day",
                        "role": "target",
                        "label": "target_day",
                        "start_inclusive": "2026-06-02",
                        "end_exclusive": "2026-06-03",
                        "timezone": "Africa/Lagos",
                    },
                ),
                "baseline_windows": (
                    {
                        "window_id": "previous_day",
                        "role": "baseline",
                        "label": "previous_day",
                        "start_inclusive": "2026-06-01",
                        "end_exclusive": "2026-06-02",
                        "timezone": "Africa/Lagos",
                    },
                ),
            },
            runtime_registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

        self.assertIn("付费金额", projected["text"])
        self.assertIn("2026-06-02", projected["text"])
        self.assertIn("2026-06-01", projected["text"])
        for internal_token in ("paid_amount", "target_day", "previous_day"):
            self.assertNotIn(internal_token, projected["text"])
        self.assertEqual(projected["target_metric"], "paid_amount")
        self.assertEqual(projected["target"]["window_id"], "target_day")
        self.assertEqual(projected["baseline"]["window_id"], "previous_day")

    def test_bound_narrative_tampering_fails_closed_at_client_projection(self):
        narrative = (
            "我对问题的理解是：核对渠道A的目标期付费金额。\n"
            "分析脉络：先确认数据权威，再核对目标窗口。\n"
            "关键发现：渠道A的目标期付费金额为10。\n"
            "最终结论：当前可以发布这一已观测结果。\n"
            "需要注意：该结论只覆盖当前目标窗口。"
        )
        package, context, claim_text = _verified_delivery_package(
            run_id="run-agent-core-closed-projection",
            final_business_summary=narrative,
            narrative_statement_bindings=(
                {
                    "excerpt": "关键发现：渠道A的目标期付费金额为10",
                    "statement_class": "verified_claim",
                    "authority_keys": ["结论1"],
                },
            ),
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
        self.assertEqual(result["status"], "failed")
        self.assertEqual(delivered["final_answer"], "")
        self.assertEqual(summary["answer_text"], "")
        self.assertEqual(summary["claims"], [])
        self.assertEqual(delivered["llm_calls"], [])
        self.assertNotIn("INJECTED", str(delivered))

    def test_self_signed_narrative_digest_cannot_bypass_authority_revalidation(self):
        from bi_agent.runtime.evidence_authority import canonical_digest

        narrative = (
            "我对问题的理解是：核对渠道A的目标期付费金额。\n"
            "分析脉络：先确认数据权威，再核对目标窗口。\n"
            "关键发现：渠道A的目标期付费金额为10。\n"
            "最终结论：当前可以发布这一已观测结果。\n"
            "需要注意：该结论只覆盖当前目标窗口。"
        )
        package, context, _ = _verified_delivery_package(
            run_id="run-agent-core-self-signed-narrative",
            final_business_summary=narrative,
            narrative_statement_bindings=(
                {
                    "excerpt": "关键发现：渠道A的目标期付费金额为10",
                    "statement_class": "verified_claim",
                    "authority_keys": ["结论1"],
                },
            ),
        )
        forged = (
            "我对问题的理解是：核对渠道A的目标期付费金额。\n"
            "分析脉络：先确认数据权威，再核对目标窗口。\n"
            "关键发现：渠道A的目标期付费金额为9,999。\n"
            "最终结论：促销活动导致付费金额上涨。\n"
            "需要注意：该结论只覆盖当前目标窗口。"
        )
        forged_bindings = [
            {
                "excerpt": "关键发现：渠道A的目标期付费金额为9,999",
                "statement_class": "verified_claim",
                "authority_keys": ["结论1"],
            },
            {
                "excerpt": "最终结论：促销活动导致付费金额上涨",
                "statement_class": "verified_claim",
                "authority_keys": ["结论1"],
            },
        ]
        package["final_answer"] = forged
        package["sections"][0]["payload"]["answer_text"] = forged
        package["sections"][0]["payload"]["final_business_summary"] = forged
        package["admin_audit"]["narrative_statement_bindings"] = forged_bindings
        binding = package["admin_audit"][
            "final_narrative_publication_binding"
        ]
        binding["status"] = "bound"
        binding["validation_errors"] = []
        binding["narrative_digest"] = canonical_digest(
            {"final_business_summary": forged}
        )
        binding["statement_bindings_digest"] = canonical_digest(
            forged_bindings
        )

        result, _ = _run_verified_package_through_core(
            package,
            context,
            thread_id="thread-agent-core-self-signed-narrative",
            run_id="run-agent-core-self-signed-narrative",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "narrative_publication_failed")
        self.assertEqual(
            result["answer_package"]["admin_audit"]["verifier"]["status"],
            "passed",
        )
        self.assertEqual(result["answer_package"]["final_answer"], "")

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
        )

        refs = {
            item["source_ref"]
            for item in result["context_manifest"]["items"]
            if item.get("can_support_claims") is True
        }
        self.assertNotIn("evidence:fake-workflow", refs)
        self.assertFalse(result["context_manifest"]["can_support_claims"])

    def test_current_run_evidence_derives_a_new_immutable_manifest(self):
        store = InMemoryConversationStore()
        manifest = {
            "manifest_id": "context-parent",
            "thread_id": "thread-manifest-version",
            "turn_id": "turn-manifest-version",
            "items": [],
            "claim_use_policy": {"can_support_bi_claim": False},
            "can_support_claims": False,
        }
        package = {
            "snapshot_id": "snapshot-1",
            "sections": [
                {
                    "payload": {
                        "claims": [{"evidence_refs": ["evidence:current"]}],
                        "evidence": [{"evidence_ref": "evidence:current"}],
                    }
                }
            ],
        }

        store.record_context_manifest(manifest)
        derived = _manifest_with_current_run_evidence(
            manifest,
            package,
        )
        store.record_context_manifest(derived)

        self.assertNotEqual(derived["manifest_id"], manifest["manifest_id"])
        self.assertEqual(
            derived["manifest_id"],
            _manifest_with_current_run_evidence(
                manifest,
                package,
            )["manifest_id"],
        )
        self.assertEqual(
            store.context_manifests["context-parent"],
            canonical_value(manifest),
        )

    def test_agent_core_creates_thread_before_initial_run_insert(self):
        store = StrictThreadStore()
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core-strict",
            run_id="run-agent-core-strict",
            user_message="Q2 比 Q1 付费金额为什么变了？",
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

    def test_pre_workflow_clarification_attempt_uses_persisted_source_envelope(self):
        calls = []
        resolution_id = "resolution-pre-workflow-1"
        attempt_run_id = "run-pre-workflow-attempt-1"

        def runner(request):
            calls.append(deepcopy(request))
            return fake_workflow(request)

        source_context = {
            "as_of": "2026-06-03T12:00:00+01:00",
            "target_date": "2026-06-02",
            "previous_day": "2026-06-01",
        }
        conflicting_attempt_context = {
            "as_of": "2026-07-13T12:00:00+01:00",
            "target_date": "2026-07-12",
            "previous_day": "2026-07-11",
        }
        question = "这个月是不是变好了？"
        choice = "按付费总金额"
        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=runner)

        waiting = core.run_message(
            thread_id="thread-pre-workflow-source",
            run_id="run-pre-workflow-source",
            user_message=question,
            analysis_context=source_context,
        )
        selected_choice = {
            "choice_id": "total_paid_amount",
            "action_kind": "bind_material_choice",
            "business_label": choice,
            "material_patch": {"target_metrics": ["paid_amount"]},
            "affected_material_slots": ["target_metric"],
        }
        store.resolve_clarification_attempt_authority = lambda **values: {
            "resolution_id": resolution_id,
            "source_run_id": values["source_run_id"],
            "attempt_run_id": values["attempt_run_id"],
            "previous_attempt_run_id": None,
            "attempt_number": 1,
            "thread_id": values["thread_id"],
            "topic_id": waiting["topic_id"],
            "owner_id": store.get_thread(values["thread_id"]).owner_id,
            "answer": values["answer"],
            "selected_option_id": values["selected_option_id"],
            "source": values["source"],
            "retry_attempt": False,
            "accepted_choice": deepcopy(selected_choice),
            "material_patch": deepcopy(selected_choice["material_patch"]),
        }
        attempt = core.run_message(
            thread_id="thread-pre-workflow-source",
            run_id=attempt_run_id,
            user_message=choice,
            clarification={
                "sourceRunId": "run-pre-workflow-source",
                "resolutionId": resolution_id,
                "attemptRunId": attempt_run_id,
                "answer": choice,
                "selectedOptionId": selected_choice["choice_id"],
                "source": "user",
                "retryAttempt": False,
            },
            analysis_context=conflicting_attempt_context,
        )

        self.assertEqual(waiting["status"], "waiting_for_clarification")
        self.assertEqual(attempt["status"], "completed")
        self.assertEqual(attempt["topic_id"], waiting["topic_id"])
        self.assertEqual(len(calls), 1)
        source_envelope = store.runs["run-pre-workflow-source"]["request"][
            "clarification_source_envelope"
        ]
        expected_source_envelope = {
            "schema_version": "clarification-source-envelope.v1",
            "source_run_id": "run-pre-workflow-source",
            "source_thread_id": "thread-pre-workflow-source",
            "source_topic_id": waiting["topic_id"],
            "source_owner_id": "user",
            "question": question,
            "analysis_context": source_context,
            "source_material": {
                "accepted_graph": [],
                "analysis_contract": {},
                "analysis_route": {},
                "original_intent": {},
                "material_slots": {},
            },
            "clarification": waiting["clarification"],
        }
        self.assertEqual(
            source_envelope,
            {
                **expected_source_envelope,
                "source_digest": canonical_digest(expected_source_envelope),
            },
        )
        request = calls[0]
        self.assertEqual(request["question"], question)
        self.assertEqual(request["user_message"], question)
        self.assertEqual(request["clarification_user_message"], choice)
        self.assertEqual(
            request["clarification_choice"],
            {
                "answer_text": choice,
                "target_metrics": ["paid_amount"],
            },
        )
        self.assertEqual(request["analysis_context"], conflicting_attempt_context)
        self.assertEqual(
            request["clarification_attempt_context"]["analysis_context"],
            conflicting_attempt_context,
        )
        self.assertEqual(
            request["clarification_attempt_context"]["original_intent"],
            {},
        )
        self.assertEqual(
            request["clarification_attempt_context"]["material_slots"],
            {},
        )

    def test_pre_workflow_clarification_attempt_rejects_missing_source_envelope(self):
        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)
        attempt_run_id = "run-pre-workflow-missing-envelope-attempt-1"
        waiting = core.run_message(
            thread_id="thread-pre-workflow-missing-envelope",
            run_id="run-pre-workflow-missing-envelope",
            user_message="这个月是不是变好了？",
            analysis_context={"as_of": "2026-06-03T12:00:00+01:00"},
        )
        source_request = store.runs[waiting["run_id"]]["request"]
        source_request.pop("clarification_source_envelope")
        source_request.update(
            {
                "thread_id": "thread-pre-workflow-missing-envelope",
                "topic_id": waiting["topic_id"],
                "question": "被顶层字段替换的问题",
                "analysis_context": {
                    "as_of": "2026-07-13T12:00:00+01:00"
                },
                "original_intent": {"target_metric": "active_users"},
            }
        )
        store.resolve_clarification_attempt_authority = lambda **values: {
            "resolution_id": "resolution-pre-workflow-missing-envelope",
            "source_run_id": values["source_run_id"],
            "attempt_run_id": values["attempt_run_id"],
            "previous_attempt_run_id": None,
            "attempt_number": 1,
            "thread_id": values["thread_id"],
            "topic_id": waiting["topic_id"],
            "owner_id": store.get_thread(values["thread_id"]).owner_id,
            "answer": values["answer"],
            "selected_option_id": values["selected_option_id"],
            "source": values["source"],
            "retry_attempt": False,
            "accepted_choice": {
                "choice_id": "total_paid_amount",
                "action_kind": "bind_material_choice",
                "business_label": "按付费总金额",
                "material_patch": {"target_metrics": ["paid_amount"]},
                "affected_material_slots": ["target_metric"],
            },
            "material_patch": {"target_metrics": ["paid_amount"]},
        }

        with self.assertRaisesRegex(
            ConversationOrchestrationError,
            "clarification_source_envelope_invalid",
        ):
            core.run_message(
                thread_id="thread-pre-workflow-missing-envelope",
                run_id=attempt_run_id,
                user_message="按付费总金额",
                clarification={
                    "sourceRunId": "run-pre-workflow-missing-envelope",
                    "resolutionId": "resolution-pre-workflow-missing-envelope",
                    "attemptRunId": attempt_run_id,
                    "answer": "按付费总金额",
                    "selectedOptionId": "total_paid_amount",
                    "source": "user",
                    "retryAttempt": False,
                },
            )
        failed_attempt = store.runs[attempt_run_id]
        self.assertEqual(failed_attempt["status"], "failed")
        self.assertEqual(
            failed_attempt["request"]["failure_reason"],
            "clarification_source_envelope_invalid",
        )

    def test_pre_workflow_source_envelope_binds_owner_and_content_digest(self):
        store = InMemoryConversationStore()
        store.create_thread(
            "thread-pre-workflow-envelope-contract",
            owner_id="owner-envelope-contract",
        )
        waiting = ConversationAgentCore(
            store,
            workflow_runner=fake_workflow,
        ).run_message(
            thread_id="thread-pre-workflow-envelope-contract",
            run_id="run-pre-workflow-envelope-contract",
            user_message="这个月是不是变好了？",
            analysis_context={"as_of": "2026-06-03T12:00:00+01:00"},
        )
        envelope = store.runs[waiting["run_id"]]["request"][
            "clarification_source_envelope"
        ]
        unsigned = dict(envelope)
        source_digest = unsigned.pop("source_digest")

        self.assertEqual(
            envelope["schema_version"],
            "clarification-source-envelope.v1",
        )
        self.assertEqual(envelope["source_owner_id"], "owner-envelope-contract")
        self.assertEqual(source_digest, canonical_digest(unsigned))

    def test_pre_workflow_attempt_rejects_envelope_authority_drift(self):
        cases = (
            "invalid_shape",
            "missing_digest",
            "empty_question",
            "owner_rebound",
            "topic_rebound",
            "source_run_rebound",
            "source_content_drift",
            "extra_signed_field",
        )
        for case in cases:
            with self.subTest(case=case):
                thread_id = f"thread-pre-workflow-envelope-{case}"
                source_run_id = f"run-pre-workflow-envelope-{case}"
                store = InMemoryConversationStore()
                store.create_thread(thread_id, owner_id=f"owner-{case}")
                core = ConversationAgentCore(store, workflow_runner=fake_workflow)
                waiting = core.run_message(
                    thread_id=thread_id,
                    run_id=source_run_id,
                    user_message="这个月是不是变好了？",
                    analysis_context={
                        "as_of": "2026-06-03T12:00:00+01:00"
                    },
                )
                attempt_run_id = f"{source_run_id}-attempt-1"
                resolution_id = f"resolution-{case}"
                store.resolve_clarification_attempt_authority = lambda **values: {
                    "resolution_id": resolution_id,
                    "source_run_id": values["source_run_id"],
                    "attempt_run_id": values["attempt_run_id"],
                    "previous_attempt_run_id": None,
                    "attempt_number": 1,
                    "thread_id": values["thread_id"],
                    "topic_id": waiting["topic_id"],
                    "owner_id": store.get_thread(values["thread_id"]).owner_id,
                    "answer": values["answer"],
                    "selected_option_id": values["selected_option_id"],
                    "source": values["source"],
                    "retry_attempt": False,
                    "accepted_choice": {
                        "choice_id": "total_paid_amount",
                        "action_kind": "bind_material_choice",
                        "business_label": "按付费总金额",
                        "material_patch": {"target_metrics": ["paid_amount"]},
                        "affected_material_slots": ["target_metric"],
                    },
                    "material_patch": {"target_metrics": ["paid_amount"]},
                }
                request = store.runs[source_run_id]["request"]
                envelope = request["clarification_source_envelope"]
                if case == "invalid_shape":
                    request["clarification_source_envelope"] = []
                elif case == "missing_digest":
                    envelope.pop("source_digest", None)
                elif case == "source_content_drift":
                    envelope["question"] = "被改写的问题"
                else:
                    if case == "empty_question":
                        envelope["question"] = "  "
                    elif case == "owner_rebound":
                        envelope["source_owner_id"] = "different-owner"
                    elif case == "topic_rebound":
                        envelope["source_topic_id"] = "different-topic"
                    elif case == "source_run_rebound":
                        envelope["source_run_id"] = "different-run"
                    else:
                        envelope["unexpected_authority"] = "unreviewed"
                    unsigned = dict(envelope)
                    unsigned.pop("source_digest", None)
                    envelope["source_digest"] = canonical_digest(unsigned)

                with self.assertRaisesRegex(
                    ConversationOrchestrationError,
                    "clarification_source_envelope_invalid",
                ):
                    core.run_message(
                        thread_id=thread_id,
                        run_id=attempt_run_id,
                        user_message="按付费总金额",
                        clarification={
                            "sourceRunId": source_run_id,
                            "resolutionId": resolution_id,
                            "attemptRunId": attempt_run_id,
                            "answer": "按付费总金额",
                            "selectedOptionId": "total_paid_amount",
                            "source": "user",
                            "retryAttempt": False,
                        },
                    )

    def test_agent_core_failed_workflow_still_returns_context_manifest(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_failed_workflow)

        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-failed-workflow",
            user_message="Q2 比 Q1 付费金额为什么变了？",
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

    def test_agent_core_passes_resolution_choice_to_workflow(self):
        captured: dict[str, object] = {}
        resolution_id = "resolution-outlier-strategy-1"
        attempt_run_id = "run-outlier-strategy-attempt-1"
        answer = "按日粒度，移除贡献最大的正向日期后复算，不做订单级明细剔除。"
        selected_choice = {
            "choice_id": "daily_remove_top_positive_day",
            "action_kind": "bind_material_choice",
            "business_label": "按日移除最大正向日",
            "material_patch": {
                "outlier_removal_strategy": "daily_remove_top_positive_day"
            },
            "affected_material_slots": ["outlier_removal_strategy"],
        }

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
        store.resolve_clarification_attempt_authority = lambda **values: {
            "resolution_id": resolution_id,
            "source_run_id": values["source_run_id"],
            "attempt_run_id": values["attempt_run_id"],
            "previous_attempt_run_id": None,
            "attempt_number": 1,
            "thread_id": values["thread_id"],
            "topic_id": waiting["topic_id"],
            "owner_id": store.get_thread(values["thread_id"]).owner_id,
            "answer": values["answer"],
            "selected_option_id": values["selected_option_id"],
            "source": values["source"],
            "retry_attempt": False,
            "accepted_choice": deepcopy(selected_choice),
            "material_patch": deepcopy(selected_choice["material_patch"]),
        }
        attempt = core.run_message(
            thread_id="thread-clarification-choice",
            run_id=attempt_run_id,
            user_message=answer,
            clarification={
                "sourceRunId": "run-waiting-choice",
                "resolutionId": resolution_id,
                "attemptRunId": attempt_run_id,
                "answer": answer,
                "selectedOptionId": selected_choice["choice_id"],
                "source": "user",
                "retryAttempt": False,
            },
        )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(waiting["status"], "waiting_for_clarification")
        self.assertEqual(attempt["status"], "completed")
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
            "--user-id",
            "user",
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
        ), patch(
            "bi_agent.conversation.agent_core._conversation_llm_from_env",
            return_value=object(),
        ):
            core = ConversationAgentCore.from_environment()

        self.assertIsInstance(core.analysis_runtime, AnalysisRuntime)
        self.assertIs(core.evidence_resolver, core.analysis_runtime.evidence_resolver)

    def test_agent_core_real_llm_environment_rejects_missing_or_invalid_provider_config(self):
        import os

        from bi_agent.runtime.llm_client import LLMConfigurationError

        invalid_configs = (
            ({}, "missing_llm_model"),
            (
                {
                    "WAJE_LLM_PROVIDER": "unsupported-provider",
                    "WAJE_LLM_MODEL": "configured-model",
                    "WAJE_LLM_API_KEY": "configured-key",
                },
                "unsupported_llm_provider",
            ),
        )

        for environment, expected_reason in invalid_configs:
            with self.subTest(expected_reason=expected_reason), patch.dict(
                os.environ,
                environment,
                clear=True,
            ), patch(
                "bi_agent.conversation.agent_core.PostgresConversationStore.from_env",
                return_value=InMemoryConversationStore(),
            ), self.assertRaisesRegex(LLMConfigurationError, expected_reason):
                ConversationAgentCore.from_environment()

    def test_agent_core_real_llm_environment_types_provider_initialization_failure(self):
        from bi_agent.runtime.llm_client import (
            LLMConfigurationError,
            OpenAICompatibleLLMClient,
        )

        with patch(
            "bi_agent.conversation.agent_core.PostgresConversationStore.from_env",
            return_value=InMemoryConversationStore(),
        ), patch.object(
            OpenAICompatibleLLMClient,
            "from_env",
            side_effect=RuntimeError("provider-constructor-failed"),
        ), self.assertRaisesRegex(
            LLMConfigurationError,
            "llm_client_initialization_failed",
        ) as caught:
            ConversationAgentCore.from_environment()

        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_real_clickhouse_core_refreshes_trusted_release_snapshots_per_plan(self):
        from tests.phase7.test_conversation_persistence import (
            _release_ref,
            _release_snapshot_payload,
        )

        store = InMemoryConversationStore()
        with patch(
            "bi_agent.conversation.agent_core.PostgresConversationStore.from_env",
            return_value=store,
        ), patch(
            "bi_agent.conversation.agent_core._conversation_llm_from_env",
            return_value=object(),
        ):
            core = ConversationAgentCore.from_environment()

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
        )

        self.assertFalse(review["real_clickhouse_verified"])
        self.assertIn("missing_clickhouse_result_refs", review["issues"])

    def test_live_harness_pauses_for_human_clarification(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import run_case

        calls = []
        manifest = {
            "manifest_id": "context-auto-clarification",
            "can_support_claims": True,
            "items": [],
        }

        class Core:
            store = _UnitRuntimeStore()
            evidence_resolver = None

            def run_message(self, **kwargs):
                calls.append(kwargs)
                return {
                    "status": "waiting_for_clarification",
                    "run_id": "run-auto-1",
                    "topic_id": "topic-auto",
                    "intent": "new_topic",
                    "topic_relation": "new_topic",
                    "context_manifest": manifest,
                    "clarification": {
                        "recommended_assumption": {
                            "option": "使用受支持口径继续"
                        },
                        "questions": [{
                            "question": "是否使用推荐口径？",
                            "options": ["使用受支持口径继续（推荐）"],
                        }],
                    },
                }

        case = {
            "id": "auto-clarification",
            "turns": [{
                "user": "昨天付费金额为什么变化？",
                "expect": {"allow_clarification": True},
            }],
        }
        with TemporaryDirectory() as tmpdir:
            output = run_case(Core(), case, Path(tmpdir))
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["status"], "waiting_for_clarification")
        self.assertEqual(output["final_turn_status"], "waiting_for_clarification")
        self.assertIsNone(output["real_clickhouse_verified"])

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
            evidence_resolver=PartialResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
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
            evidence_resolver=ContractPartialResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
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
            evidence_resolver=ShiftedResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={
                "target_date": "2026-06-02",
                "pattern_history_start": "2026-01-01",
            },
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
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
            evidence_resolver=ClaimResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
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
            evidence_resolver=context["evidence_resolver"],
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
        )

        self.assertFalse(review["runtime_correctness"]["all_claims_traceable"])
        self.assertIn(
            f"untraceable_verified_claim:{forged['claim_ref']}",
            review["issues"],
        )

    def test_live_harness_reports_missing_persisted_claim_resolver_explicitly(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-missing-persisted-claim-resolver",
        )
        manifest = package["admin_audit"]["context_manifest"]
        package["verified_claims"] = [
            package["admin_audit"]["verified_claims"][0]
        ]

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest=manifest),
            evidence_resolver=context["evidence_resolver"],
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
        )

        self.assertFalse(review["runtime_correctness"]["all_claims_traceable"])
        self.assertIn(
            "missing_verified_claim_authority_resolver",
            review["issues"],
        )

    def test_live_harness_records_persisted_claim_resolver_errors(self):
        from tools.phase7.run_live_conversation_system_test import (
            _real_clickhouse_review,
        )

        package, context, _ = _verified_delivery_package(
            run_id="run-persisted-claim-resolver-error",
        )
        manifest = package["admin_audit"]["context_manifest"]
        package["verified_claims"] = [
            package["admin_audit"]["verified_claims"][0]
        ]
        base = context["evidence_resolver"]

        class FailingClaimResolver:
            def __getattr__(self, name):
                return getattr(base, name)

            def resolve_verified_claim(self, claim_ref):
                raise RuntimeError("database unavailable")

            def resolve_claim_provenance(self, record_ref):
                raise RuntimeError("database unavailable")

        review = _real_clickhouse_review(
            self._persisted_runtime_result(package, context_manifest=manifest),
            evidence_resolver=FailingClaimResolver(),
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
        )

        self.assertFalse(review["runtime_correctness"]["all_claims_traceable"])
        self.assertIn(
            "verified_claim_authority_error:RuntimeError",
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
            evidence_resolver=context["evidence_resolver"],
            required_datasets=("paid_order_success",),
            analysis_context={"target_date": "2026-06-02"},
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
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
            evidence_resolver=LegacyResolver(),
            runtime_authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
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
        )

        self.assertFalse(review["real_clickhouse_verified"])
        self.assertIn(
            "missing_runtime_authority_resolver",
            review["issues"],
        )

    def test_live_harness_fails_closed_without_persisted_evidence_authority(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import run_case

        store = InMemoryConversationStore()
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)
        case = {
            "id": "real_clickhouse_requires_refs",
            "turns": [{"user": "Q2 比 Q1 付费金额为什么变了？", "expect": {}}],
        }

        with TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(
                RuntimeError,
                "^eval_runtime_evidence_authority_unavailable$",
            ):
                run_case(
                    core,
                    case,
                    Path(tmpdir),
                )

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

    def test_live_harness_uses_internal_artifact_content_with_run_matched_contract_authority(self):
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

        audited = _runtime_audit_package(
            result,
            authority_resolver=(
                self._run_matched_runtime_authority_resolver(package)
            ),
        )

        self.assertEqual(audited["final_answer"], "内部完整答案")
        self.assertEqual(
            audited["sections"][1]["payload"]["evidence"][0]["evidence_ref"],
            "segment:authoritative",
        )
        self.assertEqual(
            result["answer_package"]["final_answer"],
            "客户端安全答案",
        )
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

    def test_live_harness_projects_quality_from_run_matched_internal_artifact(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import _runtime_quality_review

        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            artifact_path = artifact_root / "phase-7" / "run-1" / "answer_package.json"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "quality_gate": {
                            "direct_answer": True,
                            "business_insight_present": True,
                            "followups_one_intent": False,
                            "has_verified_claims": True,
                            "verified_claim_preserved": True,
                            "display_status": "ready_with_warnings",
                            "repairable_warnings": ["missing_business_interpretation"],
                            "risk_flags": ["causal_wording_risk"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            review = _runtime_quality_review(
                {
                    "run_id": "run-1",
                    "artifact_path": str(artifact_path),
                    "answer_package": {"quality_gate": {}},
                },
                artifact_root=artifact_root,
            )

        self.assertEqual(review["display_status"], "ready_with_warnings")
        self.assertEqual(
            review["final_answer_audit_warnings"],
            ["missing_business_interpretation"],
        )
        self.assertEqual(review["risk_markers"], ["causal_wording_risk"])

    def test_live_harness_falls_back_when_internal_quality_projection_is_incomplete(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import _runtime_quality_review

        complete_gate = {
            "direct_answer": True,
            "business_insight_present": True,
            "followups_one_intent": True,
            "has_verified_claims": True,
            "verified_claim_preserved": True,
            "repairable_warnings": [],
        }
        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            artifact_path = (
                artifact_root
                / "phase-7"
                / "run-incomplete-quality"
                / "answer_package.json"
            )
            artifact_path.parent.mkdir(parents=True)
            for missing_field in complete_gate:
                with self.subTest(missing_field=missing_field):
                    internal_gate = {
                        **complete_gate,
                        "display_status": "internal_incomplete",
                        "repairable_warnings": ["internal_warning"],
                    }
                    internal_gate.pop(missing_field)
                    artifact_path.write_text(
                        json.dumps(
                            {
                                "run_id": "run-incomplete-quality",
                                "quality_gate": internal_gate,
                            }
                        ),
                        encoding="utf-8",
                    )
                    review = _runtime_quality_review(
                        {
                            "run_id": "run-incomplete-quality",
                            "artifact_path": str(artifact_path),
                            "answer_package": {
                                "quality_gate": {
                                    **complete_gate,
                                    "display_status": "public_complete",
                                    "repairable_warnings": ["public_warning"],
                                }
                            },
                        },
                        artifact_root=artifact_root,
                    )
                    self.assertEqual(review["display_status"], "public_complete")
                    self.assertEqual(
                        review["final_answer_audit_warnings"],
                        ["public_warning"],
                    )

    def test_live_harness_quality_projection_requires_exact_field_types(self):
        from tools.phase7.run_live_conversation_system_test import (
            _has_valid_quality_projection,
        )

        complete_gate = {
            "direct_answer": True,
            "business_insight_present": True,
            "followups_one_intent": False,
            "has_verified_claims": True,
            "verified_claim_preserved": True,
            "repairable_warnings": [],
        }
        malformed_values = {
            "direct_answer": 1,
            "business_insight_present": "true",
            "followups_one_intent": None,
            "has_verified_claims": "false",
            "verified_claim_preserved": 0,
            "repairable_warnings": (),
        }
        for field, malformed in malformed_values.items():
            with self.subTest(field=field):
                self.assertFalse(
                    _has_valid_quality_projection(
                        {
                            "quality_gate": {
                                **complete_gate,
                                field: malformed,
                            }
                        }
                    )
                )

    def test_live_harness_rejects_internal_quality_artifact_outside_root(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import _runtime_quality_review

        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            artifact_root.mkdir()
            outside_path = Path(tmpdir) / "outside.json"
            outside_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "quality_gate": {
                            "repairable_warnings": ["untrusted_warning"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            review = _runtime_quality_review(
                {
                    "run_id": "run-1",
                    "artifact_path": str(outside_path),
                    "answer_package": {
                        "quality_gate": {
                            "display_status": "public_delivery",
                            "repairable_warnings": ["public_warning"],
                        }
                    },
                },
                artifact_root=artifact_root,
            )

        self.assertEqual(review["display_status"], "public_delivery")
        self.assertEqual(review["final_answer_audit_warnings"], ["public_warning"])

    def test_live_harness_rejects_internal_quality_artifact_for_another_run(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import _runtime_quality_review

        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            artifact_path = artifact_root / "phase-7" / "run-2" / "answer_package.json"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(
                json.dumps(
                    {
                        "run_id": "forged-run",
                        "quality_gate": {
                            "repairable_warnings": ["forged_warning"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            review = _runtime_quality_review(
                {
                    "run_id": "run-2",
                    "artifact_path": str(artifact_path),
                    "answer_package": {
                        "quality_gate": {
                            "display_status": "public_delivery",
                            "repairable_warnings": ["public_warning"],
                        }
                    },
                },
                artifact_root=artifact_root,
            )

        self.assertEqual(review["display_status"], "public_delivery")
        self.assertEqual(review["final_answer_audit_warnings"], ["public_warning"])

    def test_live_harness_anchors_relative_internal_artifact_to_repository_root(self):
        import os
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import _runtime_quality_review

        with TemporaryDirectory() as tmpdir:
            fake_path = (
                Path(tmpdir)
                / "artifacts"
                / "phase-7"
                / "cwd-forgery"
                / "answer_package.json"
            )
            fake_path.parent.mkdir(parents=True)
            fake_path.write_text(
                json.dumps(
                    {
                        "run_id": "cwd-forgery",
                        "quality_gate": {
                            "repairable_warnings": ["cwd_untrusted"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmpdir)
                review = _runtime_quality_review(
                    {
                        "run_id": "cwd-forgery",
                        "artifact_path": (
                            "artifacts/phase-7/cwd-forgery/answer_package.json"
                        ),
                        "answer_package": {
                            "quality_gate": {
                                "display_status": "public_delivery",
                                "repairable_warnings": ["public_warning"],
                            }
                        },
                    }
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(review["display_status"], "public_delivery")
        self.assertEqual(review["final_answer_audit_warnings"], ["public_warning"])

    def test_live_harness_falls_back_from_malformed_internal_quality_codes(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import _runtime_quality_review

        malformed_values = {
            "repairable_warnings": "split-me",
            "issues": {"nested": "do-not-project"},
            "risk_flags": 42,
            "final_summary_display_warnings": ["valid", {"nested": "invalid"}],
        }
        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            artifact_path = artifact_root / "phase-7" / "run-malformed" / "answer_package.json"
            artifact_path.parent.mkdir(parents=True)
            for field, malformed in malformed_values.items():
                with self.subTest(field=field):
                    artifact_path.write_text(
                        json.dumps(
                            {
                                "run_id": "run-malformed",
                                "quality_gate": {field: malformed},
                            }
                        ),
                        encoding="utf-8",
                    )
                    review = _runtime_quality_review(
                        {
                            "run_id": "run-malformed",
                            "artifact_path": str(artifact_path),
                            "answer_package": {
                                "quality_gate": {
                                    "display_status": "public_delivery",
                                    "repairable_warnings": ["public_warning"],
                                }
                            },
                        },
                        artifact_root=artifact_root,
                    )
                    self.assertEqual(review["display_status"], "public_delivery")
                    self.assertEqual(
                        review["final_answer_audit_warnings"],
                        ["public_warning"],
                    )
                    self.assertNotIn("nested", json.dumps(review))

    def test_live_harness_rejects_non_string_run_id_for_internal_quality(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import _runtime_quality_review

        with TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / "artifacts"
            artifact_path = artifact_root / "phase-7" / "numeric-run" / "answer_package.json"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(
                json.dumps(
                    {
                        "run_id": 42,
                        "quality_gate": {
                            "repairable_warnings": ["numeric_run_untrusted"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            review = _runtime_quality_review(
                {
                    "run_id": 42,
                    "artifact_path": str(artifact_path),
                    "answer_package": {
                        "quality_gate": {
                            "display_status": "public_delivery",
                            "repairable_warnings": ["public_warning"],
                        }
                    },
                },
                artifact_root=artifact_root,
            )

        self.assertEqual(review["display_status"], "public_delivery")
        self.assertEqual(review["final_answer_audit_warnings"], ["public_warning"])

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

    def test_expectation_review_accepts_legal_zero_claim_terminal_boundary(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {}},
            {"intent": "analysis", "topic_relation": "new_topic"},
            {
                "intent": "analysis",
                "topic_relation": "new_topic",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "现有快照在固定时钟下不可用，未形成业务结论。",
                                "claims": [],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "manifest_id": "context-no-claim",
                    "can_support_claims": False,
                    "items": [],
                },
            },
            [],
        )

        self.assertEqual(review["claim_evidence_review"]["claim_count"], 0)
        self.assertTrue(review["claim_evidence_review"]["passed"])
        self.assertTrue(review["claim_support_policy_passed"])
        self.assertTrue(review["passed"])

    def test_expectation_review_rejects_claim_when_manifest_denies_claim_support(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {}},
            {"intent": "analysis", "topic_relation": "new_topic"},
            {
                "intent": "analysis",
                "topic_relation": "new_topic",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "claims": [
                                    {
                                        "text": "渠道收入上升。",
                                        "evidence_refs": ["evidence:market-1"],
                                        "context_manifest_ref": "context-denied",
                                        "reuse_decisions": [
                                            {
                                                "decision": "rerun",
                                                "result_ref": "result:market-1",
                                                "reason": "current_run_evidence",
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "manifest_id": "context-denied",
                    "can_support_claims": False,
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "evidence:market-1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertEqual(review["claim_evidence_review"]["claim_count"], 1)
        self.assertTrue(review["claim_evidence_review"]["passed"])
        self.assertFalse(review["claim_support_policy_passed"])
        self.assertFalse(review["passed"])

    def test_expectation_review_rejects_string_false_claim_support_with_valid_refs(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {}},
            {"intent": "analysis", "topic_relation": "new_topic"},
            {
                "intent": "analysis",
                "topic_relation": "new_topic",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "claims": [
                                    {
                                        "text": "渠道收入上升。",
                                        "evidence_refs": ["evidence:market-typed"],
                                        "context_manifest_ref": "context-typed",
                                        "reuse_decisions": [
                                            {
                                                "decision": "rerun",
                                                "result_ref": "result:market-typed",
                                                "reason": "current_run_evidence",
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "manifest_id": "context-typed",
                    "can_support_claims": "false",
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "evidence:market-typed",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertEqual(review["claim_evidence_review"]["claim_count"], 1)
        self.assertTrue(review["claim_evidence_review"]["passed"])
        self.assertFalse(review["context_manifest_can_support_claims"])
        self.assertFalse(review["claim_support_policy_passed"])
        self.assertFalse(review["passed"])

    def test_expectation_review_requires_explicit_false_for_zero_claim_terminal(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        for raw_value in ("false", 0, True, None):
            with self.subTest(raw_value=raw_value):
                manifest = {
                    "manifest_id": "context-zero-claim-typed",
                    "items": [],
                }
                if raw_value is not None:
                    manifest["can_support_claims"] = raw_value
                review = _expectation_review(
                    {"expect": {}},
                    {"intent": "analysis", "topic_relation": "new_topic"},
                    {
                        "intent": "analysis",
                        "topic_relation": "new_topic",
                        "answer_package": {
                            "sections": [{"payload": {"claims": []}}]
                        },
                        "context_manifest": manifest,
                    },
                    [],
                )

                self.assertEqual(
                    review["claim_evidence_review"]["claim_count"],
                    0,
                )
                self.assertFalse(review["claim_support_policy_passed"])
                self.assertFalse(review["passed"])

    def test_live_harness_passes_absolute_core_artifact_root_outside_repo_cwd(self):
        import os
        from tempfile import TemporaryDirectory

        from tools.phase7 import run_live_conversation_system_test as system_test

        captured: list[dict] = []

        class Core:
            evidence_resolver = None

            def run_message(self, **kwargs):
                captured.append(kwargs)
                return {
                    "status": "failed",
                    "run_id": "run-absolute-artifact-root",
                    "topic_id": "topic-absolute-artifact-root",
                    "intent": "analysis",
                    "topic_relation": "new_topic",
                    "failure_reason": "contract_partial",
                    "answer_package": None,
                    "context_manifest": {
                        "manifest_id": "context-absolute-artifact-root",
                        "can_support_claims": False,
                        "items": [],
                    },
                    "accepted_graph": [],
                    "artifact_path": "",
                    "llm_calls": [],
                }

        case = {
            "id": "absolute-core-artifact-root",
            "turns": [{"user": "检查现有数据边界。", "expect": {}}],
        }
        with TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            repository_root = temp_root / "repository"
            outside_cwd = temp_root / "outside-cwd"
            repository_root.mkdir()
            outside_cwd.mkdir()
            previous_cwd = Path.cwd()
            try:
                os.chdir(outside_cwd)
                with patch.object(system_test, "ROOT", repository_root):
                    system_test.run_case(
                        Core(),
                        case,
                        temp_root / "eval-artifacts",
                    )
            finally:
                os.chdir(previous_cwd)

        expected = str((repository_root / "artifacts" / "phase-7").resolve())
        self.assertEqual(captured[0]["artifact_root"], expected)
        self.assertTrue(Path(captured[0]["artifact_root"]).is_absolute())

    def test_real_live_harness_reviews_with_store_persisted_evidence_resolver(self):
        from tempfile import TemporaryDirectory

        from tools.phase7 import run_live_conversation_system_test as system_test

        execution_only_resolver = object()
        persisted_evidence_resolver = object()

        class Store:
            def runtime_evidence_resolver(self):
                return persisted_evidence_resolver

        class Core:
            store = Store()
            evidence_resolver = execution_only_resolver

            def run_message(self, **kwargs):
                return {
                    "status": "completed",
                    "run_id": "run-persisted-evidence-review",
                    "topic_id": "topic-persisted-evidence-review",
                    "intent": "analysis",
                    "topic_relation": "new_topic",
                    "failure_reason": None,
                    "answer_package": None,
                    "context_manifest": {
                        "manifest_id": "context-persisted-evidence-review",
                        "can_support_claims": False,
                        "items": [],
                    },
                    "accepted_graph": [],
                    "artifact_path": "",
                    "llm_calls": [],
                }

        runtime_review = {
            "required": True,
            "real_clickhouse_verified": False,
            "clickhouse_result_refs": [],
            "observed_datasets": [],
            "required_datasets": [],
            "runtime_correctness": {
                "all_required_queries_complete": False,
                "all_capabilities_bound": False,
                "all_claims_traceable": True,
            },
            "issues": ["missing_clickhouse_result_refs"],
        }
        with TemporaryDirectory() as tmpdir, patch.object(
            system_test,
            "_real_clickhouse_review",
            return_value=runtime_review,
        ) as review:
            system_test.run_case(
                Core(),
                {
                    "id": "persisted-evidence-review",
                    "turns": [{"user": "检查证据边界。", "expect": {}}],
                },
                Path(tmpdir),
            )

        self.assertIs(
            review.call_args.kwargs["evidence_resolver"],
            persisted_evidence_resolver,
        )

    def test_live_harness_rejects_initial_quality_artifact_from_sibling_suite(self):
        from tempfile import TemporaryDirectory

        from tools.phase7 import run_live_conversation_system_test as system_test

        public_gate = {
            "direct_answer": True,
            "business_insight_present": True,
            "followups_one_intent": False,
            "has_verified_claims": False,
            "verified_claim_preserved": False,
            "repairable_warnings": ["public_warning"],
            "display_status": "public_complete",
        }
        sibling_gate = {
            **public_gate,
            "repairable_warnings": ["sibling_warning"],
            "display_status": "sibling_untrusted",
        }

        with TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            repository_root = temp_root / "repository"
            sibling_path = (
                repository_root
                / "artifacts"
                / "other-suite"
                / "run-sibling-initial"
                / "answer_package.json"
            )
            sibling_path.parent.mkdir(parents=True)
            sibling_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-sibling-initial",
                        "quality_gate": sibling_gate,
                    }
                ),
                encoding="utf-8",
            )

            class Core:
                store = _UnitRuntimeStore()
                evidence_resolver = None

                def run_message(self, **kwargs):
                    Path(kwargs["artifact_root"]).mkdir(parents=True, exist_ok=True)
                    return {
                        "status": "completed",
                        "run_id": "run-sibling-initial",
                        "topic_id": "topic-sibling-initial",
                        "intent": "analysis",
                        "topic_relation": "new_topic",
                        "answer_package": {"quality_gate": public_gate},
                        "context_manifest": {
                            "manifest_id": "context-sibling-initial",
                            "can_support_claims": False,
                            "items": [],
                        },
                        "accepted_graph": [],
                        "artifact_path": str(sibling_path),
                        "llm_calls": [],
                    }

            with patch.object(system_test, "ROOT", repository_root):
                output = system_test.run_case(
                    Core(),
                    {
                        "id": "sibling-initial-quality",
                        "turns": [{"user": "检查证据边界。", "expect": {}}],
                    },
                    temp_root / "eval-artifacts",
                )

        review = output["turns"][0]["quality_review"]
        self.assertEqual(review["display_status"], "public_complete")
        self.assertEqual(
            review["final_answer_audit_warnings"],
            ["public_warning"],
        )
    def test_live_harness_uses_fresh_thread_for_each_case_run(self):
        from tools.phase7.run_live_conversation_system_test import _case_thread_id

        first = _case_thread_id({"id": "q2_q1_wajespecial_long_followup"})
        second = _case_thread_id({"id": "q2_q1_wajespecial_long_followup"})

        self.assertTrue(first.startswith("live-q2_q1_wajespecial_long_followup-"))
        self.assertTrue(second.startswith("live-q2_q1_wajespecial_long_followup-"))
        self.assertNotEqual(first, second)

    def test_live_harness_uses_real_dependency_artifact_directory(self):
        from tools.phase7.run_live_conversation_system_test import _default_artifact_dir

        self.assertEqual(
            _default_artifact_dir(),
            Path("artifacts/phase7/live-conversation-real"),
        )

    def test_live_harness_main_always_initializes_real_dependencies(self):
        from tools.phase7 import run_live_conversation_system_test as system_test

        with patch.object(system_test, "load_env_file"), patch.object(
            system_test,
            "resolve_cli_cases",
            return_value=[],
        ), patch.object(
            system_test.ConversationAgentCore,
            "from_environment",
            return_value=object(),
        ) as core_factory, patch("sys.stdout", new=StringIO()):
            system_test.main([
                "--cases",
                "unused.yaml",
                "--case",
                "single-case",
            ])

        core_factory.assert_called_once_with()


def _claim_scoped_package_projection_scenario(*, run_id):
    from bi_agent.runtime.analysis_contracts import (
        AnalysisContract,
        analysis_contract_signature,
    )
    from bi_agent.runtime.claim_provenance import (
        build_trusted_claim_provenance_record,
    )
    from tests.phase7.test_analysis_runtime_persistence import (
        _evidence_for_binding,
    )
    from tests.phase7.test_analysis_runtime_reuse import (
        _physical_reuse_result_fixture,
    )

    runtime, current, runtime_request = _physical_reuse_result_fixture(
        current_run_id=run_id,
    )
    analysis_ref = current.analysis_contract.analysis_contract_id
    _, fresh_context = verified_dimension_scan_asset(
        rows=(
            {
                "window_id": "pattern_history",
                "period": "2026-06-01",
                "channel": "B",
                "amount": 21.0,
            },
        ),
        required_fields=("window_id", "amount", "channel"),
        resolved_windows={
            "pattern_history": {
                "start_inclusive": "2026-06-01",
                "end_exclusive": "2026-06-02",
                "timezone": "Africa/Lagos",
            }
        },
        query_ref=f"query:{run_id}:fresh-channel",
        snapshot_ref=f"snapshot:{run_id}:fresh-channel",
        analysis_contract_ref=analysis_ref,
    )

    runtime_authority = runtime.evidence_resolver
    fresh_authority = fresh_context["evidence_resolver"]

    class CompositeAuthority:
        def __init__(self, *authorities):
            self.authorities = authorities

        def _resolve(self, method, ref):
            for authority in self.authorities:
                value = getattr(authority, method)(ref)
                if value is not None:
                    return value
            return None

        def resolve_query_execution(self, ref):
            return self._resolve("resolve_query_execution", ref)

        def resolve_query_execution_record(self, ref):
            return self._resolve("resolve_query_execution_record", ref)

        def resolve_rows(self, ref):
            return self._resolve("resolve_rows", ref)

        def resolve_rows_record(self, ref):
            return self._resolve("resolve_rows_record", ref)

        def resolve_snapshot(self, ref):
            return self._resolve("resolve_snapshot", ref)

        def resolve_completeness(self, ref):
            return self._resolve("resolve_completeness", ref)

        def resolve_latest_completeness(self, ref):
            return self._resolve("resolve_latest_completeness", ref)

        def resolve_capability_binding(self, ref):
            return self._resolve("resolve_capability_binding", ref)

    class CompositeRowsLoader:
        def __init__(self, *loaders):
            self.loaders = loaders

        def load_rows(self, storage_ref):
            for loader in self.loaders:
                rows = loader.load_rows(storage_ref)
                if rows is not None:
                    return rows
            return None

    class CompositeReleaseResolver:
        def __init__(self, *resolvers):
            self.resolvers = resolvers

        def resolve_dataset_release(self, release_ref):
            for resolver in self.resolvers:
                try:
                    return resolver.resolve_dataset_release(release_ref)
                except KeyError:
                    continue
            raise KeyError(release_ref)

    evidence_resolver = CompositeAuthority(runtime_authority, fresh_authority)
    rows_loader = CompositeRowsLoader(
        runtime.rows_loader,
        fresh_context["rows_loader"],
    )
    release_resolver = CompositeReleaseResolver(
        runtime.release_resolver,
        fresh_context["release_resolver"],
    )
    registry = runtime.registry
    source_topic_id = runtime_request["topic_id"]
    source_candidates = list(runtime.store.result_refs[source_topic_id])
    runtime.store.result_refs[source_topic_id] = []
    current_records = current.persistence_records
    current_binding = current_records["capability_binding_records"][0]
    fresh_binding = fresh_authority.resolve_capability_binding(
        fresh_context["binding_manifest_ref"]
    )

    fresh_result_refs = (
        *fresh_binding.result_refs,
        *fresh_binding.validation_result_refs,
    )
    fresh_query_records = tuple(
        fresh_authority.resolve_query_execution(ref) for ref in fresh_result_refs
    )
    fresh_rows_records = tuple(
        fresh_authority.resolve_rows(record.rows_ref)
        for record in fresh_query_records
    )
    fresh_completeness_records = tuple(
        fresh_authority.resolve_completeness(ref)
        for ref in (
            *fresh_binding.completeness_record_refs,
            *fresh_binding.validation_completeness_record_refs,
        )
    )
    fresh_snapshot_records = tuple(
        fresh_authority.resolve_snapshot(ref)
        for ref in dict.fromkeys(
            ref
            for record in fresh_query_records
            for ref in record.source_snapshot_refs
        )
    )
    query_records = (
        *current_records["query_execution_records"],
        *fresh_query_records,
    )
    query_contracts = tuple(record.contract for record in query_records)
    rows_records = (
        *current_records["rows_records"],
        *fresh_rows_records,
    )
    snapshot_records = tuple(
        {
            record.snapshot_ref: record
            for record in (
                *current_records["snapshot_records"],
                *fresh_snapshot_records,
            )
        }.values()
    )
    completeness_records = (
        *current_records["completeness_records"],
        *fresh_completeness_records,
    )
    bindings = (current_binding, fresh_binding)

    windows = {}
    metrics = {}
    dimensions = {}
    for contract in query_contracts:
        for window in contract.resolved_windows:
            windows.setdefault(window.window_id, window)
        for metric in contract.metric_bindings:
            metrics.setdefault((metric.metric_id, metric.dataset_id), metric)
        for dimension in contract.dimension_bindings:
            dimensions.setdefault(
                (dimension.dimension_id, dimension.dataset_id),
                dimension,
            )
    combined_contract = AnalysisContract(
        analysis_contract_id=analysis_ref,
        contract_version=current.analysis_contract.contract_version,
        question_families=tuple(
            dict.fromkeys(
                (
                    *current.analysis_contract.question_families,
                    "segment_or_factor_attribution",
                )
            )
        ),
        target_metric_refs=tuple(
            dict.fromkeys(metric.contract_ref for metric in metrics.values())
        ),
        claim_intents=tuple(
            dict.fromkeys(
                claim_type
                for binding in bindings
                for claim_type in binding.supported_claim_types
            )
        ),
        scope={
            **dict(current.analysis_contract.scope),
            "requested_dimension_ids": ("channel",),
        },
        business_timezone=current.analysis_contract.business_timezone,
        as_of=current.analysis_contract.as_of,
        resolved_windows=tuple(windows.values()),
        metric_bindings=tuple(metrics.values()),
        dimension_bindings=tuple(dimensions.values()),
        dataset_requirements=tuple(
            dict.fromkeys(
                record.snapshot.dataset_id for record in snapshot_records
            )
        ),
        capability_requirements=tuple(
            dict.fromkeys(binding.capability_id for binding in bindings)
        ),
    )
    analysis_contract = {
        **combined_contract.to_dict(),
        "contract_signature": analysis_contract_signature(combined_contract),
    }
    base_records = {
        "analysis_contract": analysis_contract,
        "query_contracts": query_contracts,
        "query_execution_records": query_records,
        "rows_records": rows_records,
        "snapshot_records": snapshot_records,
        "completeness_records": completeness_records,
        "capability_binding_records": bindings,
    }
    combined_runtime_result = replace(
        current,
        analysis_contract=combined_contract,
        query_contracts=query_contracts,
        persistence_records=base_records,
    )
    physical_decision = dict(current.reuse_decisions[0])
    evidence_refs = (
        "evidence:claim-scoped:reused-total",
        "evidence:claim-scoped:fresh-channel",
    )
    evidence = (
        {
            **_evidence_for_binding(
                current_binding,
                evidence_ref=evidence_refs[0],
            ),
            "numeric_facts": {"paid_amount": 120.0},
            "typed_payload": {"paid_amount": 120.0},
        },
        {
            **_evidence_for_binding(
                fresh_binding,
                evidence_ref=evidence_refs[1],
            ),
            "numeric_facts": {"paid_amount": 21.0},
            "typed_payload": {"paid_amount": 21.0},
        },
    )
    current_query = runtime_authority.resolve_query_execution(
        current_binding.result_refs[0]
    )
    fresh_query = fresh_authority.resolve_query_execution(
        fresh_binding.result_refs[0]
    )
    draft_claims = (
        {
            "text": "目标期总付费金额为 120。",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (evidence_refs[0],),
            "numbers": {"paid_amount": 120.0},
            "fact_selectors": {
                "paid_amount": {
                    "query_contract_ref": current_query.query_contract_ref,
                    "result_ref": current_query.result_ref,
                    "metric_id": "paid_amount",
                    "window_role": "target",
                    "window_id": "target_day",
                    "observation_key": "2026-06-02",
                    "dimensions": {},
                    "grain": list(current_query.contract.result_shape.grain),
                }
            },
        },
        {
            "text": "渠道 B 的独立历史窗口付费金额为 21。",
            "claim_type": "segment_contribution_or_mix_shift",
            "claim_strength": "observed",
            "evidence_refs": (evidence_refs[1],),
            "numbers": {"paid_amount": 21.0},
            "fact_selectors": {
                "paid_amount": {
                    "query_contract_ref": fresh_query.query_contract_ref,
                    "result_ref": fresh_query.result_ref,
                    "metric_id": "paid_amount",
                    "window_role": "target",
                    "window_id": "pattern_history",
                    "observation_key": "2026-06-01",
                    "dimensions": {"channel": "B"},
                    "grain": list(fresh_query.contract.result_shape.grain),
                }
            },
        },
    )

    def build(request):
        if request["topic_id"] != runtime_request["topic_id"]:
            raise AssertionError("claim_scoped_fixture_topic_drift")
        runtime.store.result_refs[source_topic_id] = list(source_candidates)
        leaked_provenance = build_trusted_claim_provenance_record(
            run_id=run_id,
            artifact_refs=("artifact:claim-scoped",),
            memory_refs=("memory:claim-scoped",),
            reuse_decisions=(physical_decision,),
        )
        package = build_answer_package(
            run_id=run_id,
            draft_claims=draft_claims,
            evidence=evidence,
            evidence_resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=registry,
            release_resolver=release_resolver,
            checkpoint_events=(),
            proposed_graph=("compare_periods", "segment_contribution"),
            accepted_graph=(
                "compare_periods",
                "segment_contribution",
                "answer_verify",
            ),
            rejected_or_degraded_mutations=(),
            validator_results=(),
            sql_text="SELECT paid_amount",
            sql_hash="sha256:claim-scoped",
            artifact_audit={"artifact_ref": "artifact:claim-scoped"},
            analysis_contract=analysis_contract,
            answer_text="",
            final_business_summary="",
            trusted_claim_provenance_record=leaked_provenance,
            reuse_decisions=(physical_decision,),
        )
        return package, base_records, combined_runtime_result, physical_decision

    return {
        "store": runtime.store,
        "thread_id": "thread-reuse",
        "evidence_resolver": evidence_resolver,
        "rows_loader": rows_loader,
        "runtime_registry": registry,
        "release_resolver": release_resolver,
        "analysis_runtime": runtime,
        "build": build,
    }


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
    reuse_decisions=(),
    final_business_summary=None,
    narrative_statement_bindings=None,
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
        resolved_windows["previous_day"] = {
            "start_inclusive": "2026-06-01",
            "end_exclusive": "2026-06-02",
            "timezone": "Africa/Lagos",
        }
        rows.append(
            {
                "window_id": "previous_day",
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
        "numeric_facts": dict(verified_numbers),
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
                        window_id="previous_day",
                        observation_key="2026-06-01",
                    ),
                }
            }
        else:
            raise ValueError(f"unsupported claim_selector_mode: {claim_selector_mode}")
    if claim_dimensions:
        claim["dimensions"] = dict(claim_dimensions)
    trusted_reuse_decisions = reuse_decisions or (
        {"source_ref": "asset:test", "decision": "reuse"},
    )
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
        final_business_summary=(
            ""
            if final_business_summary is None
            else final_business_summary
        ),
        narrative_statement_bindings=narrative_statement_bindings,
        trusted_claim_provenance_record=build_trusted_claim_provenance_record(
            run_id=run_id,
            artifact_refs=("artifact:test",),
            memory_refs=("memory:test",),
            reuse_decisions=trusted_reuse_decisions,
        ),
        reuse_decisions=reuse_decisions,
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
    context["analysis_contract"] = analysis_contract
    return package, context, claim_text


def test_verified_package_keeps_full_runtime_reuse_decision_in_admin_audit():
    from bi_agent.runtime.reuse_decision import (
        build_physical_query_reuse_decision_record,
    )

    decision = build_physical_query_reuse_decision_record(
        run_id="admin-reuse-decision",
        topic_id="topic:admin-reuse-decision",
        analysis_contract_ref="analysis:admin-reuse-decision:1",
        source_run_id="source-admin-reuse-decision",
        source_analysis_contract_ref="analysis:source-admin-reuse-decision:1",
        source_ref="result:source",
        source_query_contract_ref="query:source",
        source_query_execution_record_ref="query-execution:source",
        source_completeness_record_refs=("completeness:source",),
        result_ref="result:current",
        query_contract_ref="query:current",
        query_contract_signature="sha256:query-current",
        query_execution_record_ref="query-execution:current",
        completeness_record_refs=("completeness:current",),
        candidate_signature="sha256:candidate-source",
        decision="reuse",
        reason="validated_authoritative_query_chain",
    )

    package, _, _ = _verified_delivery_package(
        run_id="admin-reuse-decision",
        reuse_decisions=(decision,),
    )

    assert package["admin_audit"]["reuse_decisions"] == [decision]


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
    artifact_path=None,
):
    if artifact_path is None and with_runtime_records:
        artifact_root = Path(tempfile.gettempdir()) / "waje-bi-v2-agent-core-artifacts"
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact_file = artifact_root / f"{run_id}.json"
        artifact_file.write_text(
            json.dumps(package, ensure_ascii=False),
            encoding="utf-8",
        )
        artifact_path = str(artifact_file)
    artifact_path = artifact_path or ""
    def workflow(request):
        records = None
        if with_runtime_records:
            records = _verified_runtime_records_for_request(
                request,
                answer_package=package,
                context=context,
                artifact_path=artifact_path,
            )
            return _completed_runtime_workflow_result(
                request,
                answer_package=package,
                records=records,
                artifact_path=artifact_path,
                checkpoint_events=(),
            )
        return WorkflowRunResult(
            status="draft",
            run_id=request["run_id"],
            answer_package=package,
            artifact_path=artifact_path,
            checkpoint_events=(),
            analysis_runtime_records=None,
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


def _verified_runtime_records_for_request(
    request,
    *,
    answer_package,
    context,
    artifact_path="",
):
    from types import SimpleNamespace

    from bi_agent.runtime.analysis_contracts import analysis_contract_signature
    from bi_agent.runtime.analysis_runtime import AnalysisRuntime

    resolver = context["evidence_resolver"]
    binding = resolver.resolve_capability_binding(context["binding_manifest_ref"])
    result_refs = tuple(
        dict.fromkeys((*binding.result_refs, *binding.validation_result_refs))
    )
    query_records = tuple(
        resolver.resolve_query_execution(ref) for ref in result_refs
    )
    rows_records = tuple(
        {
            record.record_ref: record
            for query in query_records
            if (record := resolver.resolve_rows(query.rows_ref)) is not None
        }.values()
    )
    completeness_records = tuple(
        {
            record.record_ref: record
            for ref in (
                *binding.completeness_record_refs,
                *binding.validation_completeness_record_refs,
            )
            if (record := resolver.resolve_completeness(ref)) is not None
        }.values()
    )
    snapshot_records = tuple(
        {
            record.record_ref: record
            for query in query_records
            for ref in query.source_snapshot_refs
            if (record := resolver.resolve_snapshot(ref)) is not None
        }.values()
    )
    analysis_contract = context["analysis_contract"]
    base_records = {
        "analysis_contract": {
            **analysis_contract.to_dict(),
            "contract_signature": analysis_contract_signature(analysis_contract),
        },
        "query_contracts": tuple(
            {
                query.contract.query_contract_id: query.contract
                for query in query_records
            }.values()
        ),
        "query_execution_records": query_records,
        "rows_records": rows_records,
        "snapshot_records": snapshot_records,
        "completeness_records": completeness_records,
        "capability_binding_records": (binding,),
    }
    runtime_result = SimpleNamespace(
        persistence_records=base_records,
        analysis_contract=analysis_contract,
        repair_decisions=(),
        reuse_decisions=(),
    )
    return AnalysisRuntime.build_persistence_bundle(
        object.__new__(AnalysisRuntime),
        runtime_result,
        answer_package=answer_package,
        request=request,
        artifact_path=artifact_path,
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


def _completed_material_authority_for_records(request, records):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )
    from tests.phase7.test_material_authority import (
        _runtime_material_for_contract,
    )

    contract = records["analysis_contract"]
    metric_ids = [
        str(binding.get("metric_id") or "")
        for binding in contract.get("metric_bindings") or ()
        if isinstance(binding, dict) and str(binding.get("metric_id") or "")
    ]
    if not metric_ids:
        metric_ids = ["paid_amount"]
    families = list(contract.get("question_families") or ())
    scope = str((contract.get("scope") or {}).get("type") or "full_sample")
    material = _workflow_clarification_material(
        question_family=families[0],
        target_metric=metric_ids[0],
        claim_intents=tuple(contract.get("claim_intents") or ()),
        scope=scope,
    )
    return build_material_authority(
        source_run_id=request["run_id"],
        thread_id=request["thread_id"],
        topic_id=request["topic_id"],
        original_intent=material["original_intent"],
        material_slots=material["material_slots"],
        runtime_material=_runtime_material_for_contract(contract),
    )


def _authoritative_runtime_records_for_request(request):
    from tests.phase7.test_analysis_runtime_persistence import _authority_bundle

    return _authority_bundle(
        run_id=request["run_id"],
        thread_id=request["thread_id"],
        topic_id=request["topic_id"],
        analysis_contract_ref=f"analysis:{request['run_id']}:1",
    )


def _queryless_runtime_records_for_request(
    request,
    *,
    target_ref="contracts/metrics/paid-amount.metric.yaml@0.1",
):
    from bi_agent.runtime.analysis_contracts import analysis_contract_signature

    records = _authoritative_runtime_records_for_request(request)
    contract = deepcopy(records["analysis_contract"])
    contract.update(
        {
            "target_metric_refs": [target_ref],
            "claim_intents": [],
            "metric_bindings": [],
            "capability_requirements": [],
            "contract_gaps": [],
        }
    )
    contract["contract_signature"] = analysis_contract_signature(contract)
    records["analysis_contract"] = contract
    for key in (
        "query_contracts",
        "query_execution_records",
        "rows_records",
        "snapshot_records",
        "completeness_records",
        "capability_binding_records",
        "evidence_manifests",
        "context_manifests",
        "trusted_provenance_records",
        "answer_package_artifacts",
        "verified_claims",
        "claim_links",
        "repair_attempts",
    ):
        records[key] = []
    return records


def _completed_runtime_workflow_result(
    request,
    *,
    answer_package,
    records,
    **result_fields,
):
    if (
        records.get("trusted_provenance_records")
        and "artifact_path" not in result_fields
    ):
        from tests.phase7.artifact_test_support import (
            materialize_answer_package_artifact,
        )

        artifact_path, _ = materialize_answer_package_artifact(
            run_id=request["run_id"],
            answer_package=answer_package,
        )
        result_fields["artifact_path"] = artifact_path
    return WorkflowRunResult(
        status="draft",
        run_id=request["run_id"],
        answer_package=answer_package,
        analysis_runtime_records=records,
        completed_material_authority=(
            _completed_material_authority_for_records(request, records)
        ),
        **result_fields,
    )


class ContractAuthorityOnlyStore(InMemoryConversationStore):
    """Persist the completion anchor while a test isolates candidate filtering."""

    def save_analysis_runtime_records(self, **kwargs):
        self.persisted_runtime_records = kwargs
        contract = canonical_value(kwargs.get("analysis_contract") or {})
        contract_ref = str(contract.get("analysis_contract_id") or "")
        if contract_ref:
            self.analysis_runtime_authority["analysis_contract"][
                contract_ref
            ] = contract
        return "inserted"


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
