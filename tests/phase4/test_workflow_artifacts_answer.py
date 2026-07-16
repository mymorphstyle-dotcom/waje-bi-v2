import json
import tempfile
import unittest
from unittest.mock import patch

from bi_agent.runtime.answer_package import (
    _terminal_explanation_projection,
    build_answer_package,
    reverify_answer_package_for_delivery,
    scrub_answer_package_for_delivery,
    verify_answer_package,
)
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.langgraph_workflow import (
    _available_fields_for_contract_diagnostics,
    _contract_gap_diagnostics_from_state,
    _compiler_bound_context,
    _ensure_degraded_audit,
    _ensure_blocked_boundary_audit,
    _local_coverage_block_reason,
    run_pattern_workflow as _run_pattern_workflow,
)
from bi_agent.runtime.artifacts import filter_artifact_for_role
from tests.phase4.fake_llm import FakeLLMClient


def run_pattern_workflow(request=None):
    fixture_request = dict(request or {})
    fixture_request.setdefault("run_mode", "fixture")
    with patch.dict(
        "os.environ",
        {
            "WAJE_ALLOW_LEGACY_FIXTURES": "1",
            "WAJE_RUNTIME_ENV": "test",
        },
    ):
        return _run_pattern_workflow(fixture_request)


def _llm_input_payload(answer_package, task):
    call = next(
        item for item in answer_package["admin_audit"]["llm_calls"] if item["task"] == task
    )
    user_message = next(item for item in call["messages"] if item["role"] == "user")
    content = user_message["content"]
    start = content.index("<input_json>") + len("<input_json>")
    end = content.index("</input_json>")
    return json.loads(content[start:end].strip())


class WorkflowArtifactsAnswerTest(unittest.TestCase):
    def test_build_keeps_verified_comparison_when_required_formula_claim_is_rejected(self):
        evidence = (
            {
                "evidence_ref": "compare_periods:partial",
                "capability_id": "compare_periods",
                "claim_type": "comparative_change",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:compare",
                "evidence_type": "statistical_association",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": ["comparative_change"],
                "strength": "directional",
                "wording_limit": "quantified",
                "limitations": [],
                "numeric_facts": {"absolute_change": 20.0},
                "typed_payload": {
                    "absolute_change": 20.0,
                    "scope": "full_sample",
                    "time_window": "2026-06-01",
                },
            },
            {
                "evidence_ref": "driver_decomposition:partial",
                "capability_id": "driver_decomposition",
                "claim_type": "formula_component_contribution",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:driver",
                "evidence_type": "accounting_contribution",
                "supported_evidence_types": ["accounting_contribution"],
                "supported_claim_types": ["formula_component_contribution"],
                "strength": "high",
                "wording_limit": "quantified",
                "limitations": [],
                "numeric_facts": {"paid_users_contribution_share": 0.5},
                "typed_payload": {
                    "paid_users_contribution_share": 0.5,
                    "scope": "full_sample",
                    "time_window": "2026-06-01",
                },
            },
        )
        claims = (
            {
                "text": "2026年6月1日付费金额较前一天上涨20。",
                "claim_type": "comparative_change",
                "claim_strength": "observed",
                "evidence_refs": ("compare_periods:partial",),
                "numbers": {"absolute_change": 20.0},
                "scope": "full_sample",
                "time_window": "2026-06-01",
            },
            {
                "text": "单笔付费金额是主要贡献项。",
                "claim_type": "formula_component_contribution",
                "claim_strength": "quantified_contribution",
                "evidence_refs": ("driver_decomposition:partial",),
                "numbers": {"paid_users_contribution_share": 0.9},
                "scope": "full_sample",
                "time_window": "2026-06-01",
            },
        )
        projected_comparison = {
            **claims[0],
            "fact_refs": ["fact:comparison"],
            "fact_selectors": {
                "absolute_change": {"metric_id": "paid_amount"}
            },
        }

        with patch(
            "bi_agent.runtime.answer_package._claim_authority_errors",
            return_value=(),
        ), patch(
            "bi_agent.runtime.answer_package._authority_bound_claim_projections",
            return_value=((projected_comparison,), []),
        ):
            package = build_answer_package(
                run_id="partial-required-claim",
                draft_claims=claims,
                evidence=evidence,
                checkpoint_events=(),
                proposed_graph=(),
                accepted_graph=(),
                rejected_or_degraded_mutations=(),
                validator_results=(),
                sql_text="",
                sql_hash="",
                artifact_audit={},
                answer_text=(
                    "2026年6月1日付费金额较前一天上涨20。"
                    "单笔付费金额是主要贡献项。"
                ),
                claim_intent_resolution={
                    "required_claim_intents": [
                        "comparative_change",
                        "formula_component_contribution",
                    ]
                },
            )
            delivered = reverify_answer_package_for_delivery(
                package,
                evidence_resolver=None,
                rows_loader=None,
                runtime_registry=None,
            )

        verifier = package["admin_audit"]["verifier"]
        summary = package["sections"][0]["payload"]
        self.assertEqual(package["status"], "draft")
        self.assertEqual(verifier["status"], "degraded")
        self.assertEqual(len(summary["claims"]), 1)
        self.assertEqual(summary["claims"][0]["claim_type"], "comparative_change")
        self.assertEqual(summary["final_business_summary"], "")
        self.assertIn("付费金额", package["final_answer"])
        self.assertIn("因素贡献结论本轮未发布", package["final_answer"])
        self.assertNotIn("单笔付费金额是主要贡献项", package["final_answer"])
        self.assertEqual(package["quality_gate"]["truth_status"], "verified")
        self.assertEqual(package["quality_gate"]["coverage_status"], "partial")
        self.assertFalse(package["quality_gate"]["blocks_display"])
        delivered_summary = delivered["sections"][0]["payload"]
        self.assertEqual(delivered["status"], "draft")
        self.assertEqual(
            delivered["admin_audit"]["verifier"]["status"],
            "degraded",
        )
        self.assertEqual(len(delivered_summary["claims"]), 1)
        self.assertEqual(
            delivered_summary["claims"][0]["claim_type"],
            "comparative_change",
        )
        self.assertEqual(delivered_summary["final_business_summary"], "")
        self.assertIn("因素贡献结论本轮未发布", delivered["final_answer"])
        self.assertFalse(delivered["quality_gate"]["blocks_display"])

    def test_publishable_required_evidence_cannot_finish_with_zero_claims(self):
        verifier = verify_answer_package(
            draft_claims=(),
            evidence=(
                {
                    "evidence_ref": "driver_decomposition:ready",
                    "capability_id": "driver_decomposition",
                    "claim_type": "formula_component_contribution",
                    "claim_input_ready": True,
                    "binding_manifest_ref": "binding:driver",
                    "evidence_type": "accounting_contribution",
                    "supported_evidence_types": ["accounting_contribution"],
                    "supported_claim_types": ["formula_component_contribution"],
                    "strength": "high",
                    "wording_limit": "quantified",
                    "limitations": [],
                    "typed_payload": {"core_reconciliation_status": "reconciled"},
                },
            ),
            visible_limitations=(),
            required_claim_intents=("formula_component_contribution",),
            delivery_text={
                "final_explanation": {
                    "status": "degraded",
                    "explanation": "当前暂时无法形成因素贡献结论。",
                }
            },
        )

        self.assertEqual(verifier["status"], "failed")
        missing = [
            item
            for item in verifier["errors"]
            if item["code"] == "missing_required_claim"
        ]
        self.assertEqual(
            missing,
            [
                {
                    "code": "missing_required_claim",
                    "claim_type": "formula_component_contribution",
                    "status": "draft_missing",
                    "evidence_refs": ["driver_decomposition:ready"],
                    "publishable_evidence_refs": ["driver_decomposition:ready"],
                    "limitations": [],
                }
            ],
        )
        self.assertEqual(
            verifier["required_claim_obligations"],
            [
                {
                    "claim_type": "formula_component_contribution",
                    "status": "draft_missing",
                    "evidence_refs": ["driver_decomposition:ready"],
                    "publishable_evidence_refs": ["driver_decomposition:ready"],
                    "limitations": [],
                }
            ],
        )

    def test_claim_scoped_authority_projection_failure_keeps_successful_sibling(self):
        claims = (
            {
                "text": "付费金额较基线增加20。",
                "claim_type": "comparative_change",
                "claim_strength": "observed",
                "evidence_refs": ("compare:projection",),
                "numbers": {"absolute_change": 20.0},
            },
            {
                "text": "单笔付费金额是主要贡献项。",
                "claim_type": "formula_component_contribution",
                "claim_strength": "quantified_contribution",
                "evidence_refs": ("driver:projection",),
                "numbers": {"avg_order_amount_contribution": 30.0},
            },
        )
        evidence = (
            {"evidence_ref": "compare:projection", "typed_payload": {}},
            {"evidence_ref": "driver:projection", "typed_payload": {}},
        )
        projected_comparison = {
            **claims[0],
            "fact_refs": ["fact:comparison"],
            "fact_selectors": {"absolute_change": {"metric_id": "paid_amount"}},
        }
        initial_verifier = {
            "status": "passed",
            "errors": [],
            "global_errors": [],
            "claim_rejections": [],
            "required_claim_gaps": [],
            "warnings": [],
            "accepted_claim_indexes": (0, 1),
            "rejected_claim_indexes": (),
            "accepted_assumptions": [],
        }
        projected_verifier = {
            "status": "degraded",
            "errors": [
                {
                    "code": "missing_required_claim",
                    "claim_type": "formula_component_contribution",
                    "evidence_refs": ["driver:projection"],
                }
            ],
            "global_errors": [],
            "claim_rejections": [],
            "required_claim_gaps": [
                {
                    "code": "missing_required_claim",
                    "claim_type": "formula_component_contribution",
                    "evidence_refs": ["driver:projection"],
                }
            ],
            "warnings": [],
            "accepted_claim_indexes": (0,),
            "rejected_claim_indexes": (),
            "accepted_assumptions": [],
        }

        with patch(
            "bi_agent.runtime.answer_package.verify_answer_package",
            side_effect=(initial_verifier, projected_verifier),
        ), patch(
            "bi_agent.runtime.answer_package._authority_bound_claim_projections",
            return_value=(
                (projected_comparison,),
                [
                    {
                        "code": "claim_factual_projection_failed",
                        "claim_index": 1,
                        "reason": "claim_number_field_unbound",
                    }
                ],
            ),
        ):
            package = build_answer_package(
                run_id="claim-scoped-projection",
                draft_claims=claims,
                evidence=evidence,
                checkpoint_events=(),
                proposed_graph=(),
                accepted_graph=(),
                rejected_or_degraded_mutations=(),
                validator_results=(),
                sql_text="",
                sql_hash="",
                artifact_audit={},
                answer_text="付费金额较基线增加20。单笔付费金额是主要贡献项。",
                claim_intent_resolution={
                    "required_claim_intents": [
                        "comparative_change",
                        "formula_component_contribution",
                    ]
                },
            )

        summary = package["sections"][0]["payload"]
        self.assertEqual(package["status"], "draft")
        self.assertEqual(package["admin_audit"]["verifier"]["status"], "degraded")
        self.assertEqual(len(summary["claims"]), 1)
        self.assertEqual(summary["claims"][0]["claim_type"], "comparative_change")
        self.assertIn("因素贡献结论本轮未发布", package["final_answer"])
        self.assertFalse(package["quality_gate"]["blocks_display"])

    def test_rejected_draft_claim_does_not_satisfy_required_claim_completeness(self):
        verifier = verify_answer_package(
            draft_claims=(
                {
                    "text": "单笔付费金额是主要贡献项。",
                    "claim_type": "formula_component_contribution",
                    "claim_strength": "high",
                    "evidence_refs": ("driver_decomposition:ready",),
                    "numbers": {},
                },
            ),
            evidence=(
                {
                    "evidence_ref": "driver_decomposition:ready",
                    "capability_id": "driver_decomposition",
                    "claim_type": "formula_component_contribution",
                    "claim_input_ready": True,
                    "binding_manifest_ref": "binding:driver",
                    "evidence_type": "accounting_contribution",
                    "supported_evidence_types": ["accounting_contribution"],
                    "supported_claim_types": ["formula_component_contribution"],
                    "strength": "high",
                    "wording_limit": "quantified",
                    "limitations": [],
                    "typed_payload": {"core_reconciliation_status": "reconciled"},
                },
            ),
            visible_limitations=(),
            required_claim_intents=("formula_component_contribution",),
        )

        self.assertEqual(verifier["accepted_claim_indexes"], ())
        self.assertIn(
            "missing_required_claim",
            {item["code"] for item in verifier["errors"]},
        )

    def test_unavailable_required_evidence_remains_a_terminal_boundary_obligation(self):
        verifier = verify_answer_package(
            draft_claims=(),
            evidence=(
                {
                    "evidence_ref": "driver_decomposition:blocked",
                    "claim_type": "formula_component_contribution",
                    "claim_input_ready": False,
                    "binding_manifest_ref": "binding:driver",
                    "evidence_type": "insufficient",
                    "strength": "low",
                    "wording_limit": "blocked",
                    "limitations": ["driver_components_missing"],
                    "typed_payload": {},
                },
            ),
            visible_limitations=("driver_components_missing",),
            required_claim_intents=("formula_component_contribution",),
            delivery_text={
                "final_explanation": {
                    "status": "degraded",
                    "explanation": "当前因素证据不足，无法发布贡献结论。",
                    "repair_path": "补齐因素数据后继续。",
                }
            },
        )

        self.assertEqual(verifier["status"], "degraded")
        self.assertTrue(verifier["terminal_boundary_accepted"])
        self.assertEqual(
            verifier["required_claim_obligations"],
            [
                {
                    "claim_type": "formula_component_contribution",
                    "status": "evidence_degraded",
                    "evidence_refs": ["driver_decomposition:blocked"],
                    "publishable_evidence_refs": [],
                    "limitations": ["driver_components_missing"],
                }
            ],
        )
        self.assertEqual(
            verifier["required_claim_gaps"],
            [
                {
                    "code": "missing_required_claim",
                    "claim_type": "formula_component_contribution",
                    "status": "evidence_degraded",
                    "evidence_refs": ["driver_decomposition:blocked"],
                    "publishable_evidence_refs": [],
                    "limitations": ["driver_components_missing"],
                }
            ],
        )

    def test_verified_comparison_keeps_degraded_formula_obligation_visible(self):
        evidence = (
            {
                "evidence_ref": "compare_periods:ready",
                "capability_id": "compare_periods",
                "claim_type": "comparative_change",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:compare",
                "evidence_type": "statistical_association",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": ["comparative_change"],
                "strength": "directional",
                "wording_limit": "quantified",
                "limitations": [],
                "numeric_facts": {"absolute_change": 20.0},
                "typed_payload": {},
            },
            {
                "evidence_ref": "formula_decompose:degraded",
                "capability_id": "formula_decompose",
                "claim_type": "formula_component_contribution",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:formula",
                "evidence_type": "accounting_contribution",
                "supported_evidence_types": ["accounting_contribution"],
                "supported_claim_types": ["formula_component_contribution"],
                "strength": "low",
                "wording_limit": "blocked",
                "limitations": ["formula_numbers_missing"],
                "typed_payload": {},
            },
            {
                "evidence_ref": "driver_decomposition:degraded",
                "capability_id": "driver_decomposition",
                "claim_type": "formula_component_contribution",
                "claim_input_ready": False,
                "binding_manifest_ref": "binding:driver",
                "evidence_type": "insufficient",
                "strength": "low",
                "wording_limit": "blocked",
                "limitations": ["driver_components_missing"],
                "typed_payload": {},
            },
        )
        with patch(
            "bi_agent.runtime.answer_package._claim_authority_errors",
            return_value=(),
        ):
            verifier = verify_answer_package(
                draft_claims=(
                    {
                        "text": "目标日付费金额较前一日增加20。",
                        "claim_type": "comparative_change",
                        "claim_strength": "observed",
                        "evidence_refs": ("compare_periods:ready",),
                        "numbers": {"absolute_change": 20.0},
                    },
                ),
                evidence=evidence,
                visible_limitations=(
                    "formula_numbers_missing",
                    "driver_components_missing",
                ),
                required_claim_intents=(
                    "comparative_change",
                    "formula_component_contribution",
                ),
            )

        self.assertEqual(verifier["status"], "degraded")
        self.assertEqual(verifier["accepted_claim_indexes"], (0,))
        self.assertEqual(
            verifier["required_claim_obligations"],
            [
                {
                    "claim_type": "comparative_change",
                    "status": "satisfied",
                    "evidence_refs": ["compare_periods:ready"],
                    "publishable_evidence_refs": ["compare_periods:ready"],
                    "limitations": [],
                },
                {
                    "claim_type": "formula_component_contribution",
                    "status": "evidence_degraded",
                    "evidence_refs": [
                        "formula_decompose:degraded",
                        "driver_decomposition:degraded",
                    ],
                    "publishable_evidence_refs": [],
                    "limitations": [
                        "formula_numbers_missing",
                        "driver_components_missing",
                    ],
                },
            ],
        )

    def test_publishable_auxiliary_evidence_does_not_create_required_claim(self):
        verifier = verify_answer_package(
            draft_claims=(),
            evidence=(
                {
                    "evidence_ref": "rolling_window_compare:ready",
                    "claim_type": "baseline_stability",
                    "claim_input_ready": True,
                    "binding_manifest_ref": "binding:rolling",
                    "evidence_type": "statistical_association",
                    "supported_evidence_types": ["statistical_association"],
                    "supported_claim_types": ["baseline_stability"],
                    "strength": "high",
                    "wording_limit": "supported",
                    "limitations": [],
                    "typed_payload": {},
                },
            ),
            visible_limitations=(),
            required_claim_intents=("comparative_change",),
        )

        self.assertEqual(verifier["status"], "failed")
        self.assertEqual(
            verifier["required_claim_obligations"],
            [
                {
                    "claim_type": "comparative_change",
                    "status": "evidence_absent",
                    "evidence_refs": [],
                    "publishable_evidence_refs": [],
                    "limitations": [],
                }
            ],
        )

    def test_required_claim_partition_survives_build_and_delivery_reverify(self):
        evidence = (
            {
                "evidence_ref": "driver_decomposition:ready",
                "capability_id": "driver_decomposition",
                "claim_type": "formula_component_contribution",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:driver",
                "evidence_type": "accounting_contribution",
                "supported_evidence_types": ["accounting_contribution"],
                "supported_claim_types": ["formula_component_contribution"],
                "strength": "high",
                "wording_limit": "quantified",
                "limitations": [],
                "typed_payload": {"core_reconciliation_status": "reconciled"},
            },
        )
        resolution = {
            "schema_version": "claim_intent_resolution.v1",
            "required_claim_intents": ["formula_component_contribution"],
            "auxiliary_claim_intents": ["baseline_stability"],
        }
        package = build_answer_package(
            run_id="required-claim-delivery-reverify",
            draft_claims=(),
            evidence=evidence,
            checkpoint_events=(),
            proposed_graph=(),
            accepted_graph=(),
            rejected_or_degraded_mutations=(),
            validator_results=(),
            sql_text="",
            sql_hash="",
            artifact_audit={},
            final_explanation={
                "status": "degraded",
                "explanation": "当前暂时无法形成因素贡献结论。",
                "repair_path": "重新生成证据支持的因素贡献结论。",
            },
            claim_intent_resolution=resolution,
        )

        delivered = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=None,
            rows_loader=None,
            runtime_registry=None,
        )

        self.assertEqual(delivered["claim_intent_resolution"], resolution)
        self.assertIn(
            "missing_required_claim",
            {
                item["code"]
                for item in delivered["admin_audit"]["verifier"]["errors"]
            },
        )
        self.assertEqual(
            delivered["admin_audit"]["verifier"]["status"],
            "degraded",
        )
        self.assertTrue(
            delivered["admin_audit"]["verifier"]["terminal_boundary_accepted"]
        )
        self.assertFalse(delivered["quality_gate"]["blocks_display"])

    def test_authority_bound_blocked_evidence_can_project_terminal_explanation(self):
        from types import SimpleNamespace

        candidate = {
            "status": "draft",
            "final_explanation": {
                "status": "degraded",
                "explanation": "当前比较证据不足，无法发布收入变化结论。",
                "repair_path": "补齐可比较窗口后重跑。",
            },
            "admin_audit": {
                "compiler_runtime_plan": {
                    "analysis_contract": {"contract_gaps": []}
                },
                "validator_results": [{"ok": True}],
            },
        }
        evidence = ({
            "evidence_ref": "evidence:blocked",
            "wording_limit": "blocked",
            "limitations": ["window_pair_cardinality_invalid"],
            "binding_manifest_ref": "binding:blocked",
            "result_refs": ["result:blocked"],
            "completeness_record_refs": ["completeness-record:blocked"],
        },)
        binding = SimpleNamespace(
            result_refs=("result:blocked",),
            validation_result_refs=(),
            completeness_record_refs=("completeness-record:blocked",),
            validation_completeness_record_refs=(),
        )

        class Resolver:
            def resolve_capability_binding(self, ref):
                return binding if ref == "binding:blocked" else None

        with patch(
            "bi_agent.runtime.answer_package.validate_authoritative_query_chain",
            return_value=SimpleNamespace(),
        ):
            projected = _terminal_explanation_projection(
                candidate,
                evidence=evidence,
                evidence_resolver=Resolver(),
                rows_loader=object(),
                runtime_registry=object(),
                release_resolver=None,
            )

        self.assertEqual(projected["status"], "degraded")
        self.assertIn("比较证据不足", projected["explanation"])

    def test_blocked_runtime_completeness_keeps_degraded_terminal_at_zero_claims(self):
        from types import SimpleNamespace

        state = {
            "run_id": "blocked-runtime-degrade",
            "intent": {"scope": "全市场", "time_window": "昨天和前天"},
            "analysis_runtime_result": SimpleNamespace(status="blocked"),
            "draft_claims": [],
            "evidence": [],
        }

        _ensure_degraded_audit(state)

        self.assertEqual(state["draft_claims"], [])
        self.assertEqual(len(state["evidence"]), 1)

    def test_typed_terminal_gap_is_visible_with_zero_business_claims(self):
        package = build_answer_package(
            run_id="typed-terminal-gap",
            draft_claims=(),
            evidence=(),
            checkpoint_events=(),
            proposed_graph=(),
            accepted_graph=(),
            rejected_or_degraded_mutations=(),
            validator_results=(
                {"validator": "query_completeness", "ok": False, "reason": "source_unbound"},
            ),
            sql_text="",
            sql_hash="",
            artifact_audit={},
            answer_text="",
            final_business_summary="",
            final_explanation={
                "status": "blocked",
                "explanation": "当前缺少可用的数据快照，无法发布业务结论。",
                "repair_path": "注册并验收对应数据快照后继续。",
            },
            follow_up_questions=(),
            compiler_runtime_plan={
                "analysis_contract": {
                    "contract_gaps": [
                        {"gap_type": "source_unbound", "owner": "data_owner"}
                    ]
                }
            },
        )

        delivered = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=None,
            rows_loader=None,
            runtime_registry=None,
        )
        delivered_again = reverify_answer_package_for_delivery(
            delivered,
            evidence_resolver=None,
            rows_loader=None,
            runtime_registry=None,
        )

        self.assertEqual(delivered["status"], "draft")
        self.assertEqual(delivered["sections"][0]["payload"]["claims"], [])
        self.assertIn("当前缺少可用的数据快照", delivered["final_answer"])
        self.assertFalse(delivered["quality_gate"]["blocks_display"])
        self.assertEqual(delivered_again["final_answer"], delivered["final_answer"])
        self.assertEqual(delivered_again["final_explanation"], delivered["final_explanation"])

    def test_compiler_bound_context_carries_accepted_analysis_contract_clock(self):
        context = _compiler_bound_context(
            {
                "intent": {"scope": "full_sample"},
                "request": {
                    "analysis_contract": {
                        "analysis_contract_id": "analysis:clock:1",
                        "as_of": "2026-06-03T12:00:00+01:00",
                        "resolved_windows": (
                            {
                                "window_id": "target_day",
                                "start_inclusive": "2026-06-02",
                                "end_exclusive": "2026-06-03",
                                "timezone": "Africa/Lagos",
                            },
                        ),
                    }
                },
            }
        )

        self.assertEqual(context["as_of"], "2026-06-03T12:00:00+01:00")
        self.assertEqual(
            context["analysis_contract"]["analysis_contract_id"],
            "analysis:clock:1",
        )
        self.assertEqual(
            context["resolved_windows"][0]["window_id"],
            "target_day",
        )

    def test_every_publishable_claim_requires_authoritative_provenance(self):
        for strength in ("observed", "medium", "high", "strong"):
            with self.subTest(strength=strength):
                verifier = verify_answer_package(
                    draft_claims=(
                        {
                            "text": "渠道是主要驱动。",
                            "claim_strength": strength,
                            "claim_type": "segment_contribution_or_mix_shift",
                            "evidence_refs": ("segment:unbound",),
                        },
                    ),
                    evidence=(
                        {
                            "evidence_ref": "segment:unbound",
                            "wording_limit": "supported",
                            "strength": strength,
                            "evidence_type": "statistical_association",
                            "typed_payload": {},
                        },
                    ),
                    visible_limitations=(),
                )

                self.assertEqual(verifier["status"], "failed")
                self.assertIn(
                    "claim_missing_authoritative_provenance",
                    {error["code"] for error in verifier["errors"]},
                )

    def test_context_only_numeric_claim_is_not_published_without_authority(self):
        package = build_answer_package(
            run_id="context-only-numeric",
            draft_claims=(
                {
                    "text": "目标日付费金额为 42。",
                    "claim_strength": "context_only",
                    "claim_type": "comparative_change",
                    "numbers": {"paid_amount": 42},
                    "evidence_refs": ("context:42",),
                },
            ),
            evidence=(
                {
                    "evidence_ref": "context:42",
                    "evidence_type": "context_only",
                    "wording_limit": "context_only",
                    "typed_payload": {"paid_amount": 42},
                    "limitations": ("authority_backed_evidence_missing",),
                },
            ),
            checkpoint_events=(),
            proposed_graph=(),
            accepted_graph=(),
            rejected_or_degraded_mutations=(),
            validator_results=(),
            sql_text="",
            sql_hash="",
            artifact_audit={},
        )

        summary = package["sections"][0]["payload"]
        self.assertEqual(summary["claims"], [])
        self.assertEqual(summary["claim_groups"], [])
        self.assertIn("authority_backed_evidence_missing", summary["limitations"])
        self.assertEqual(package["admin_audit"]["verifier"]["status"], "failed")

    def test_verifier_failure_scrubs_all_visible_final_text(self):
        package = build_answer_package(
            run_id="scrub-rejected-final-text",
            draft_claims=({
                "text": "目标日付费金额为 42。",
                "claim_strength": "context_only",
                "claim_type": "comparative_change",
                "numbers": {"paid_amount": 42},
                "evidence_refs": ("context:42",),
            },),
            evidence=({
                "evidence_ref": "context:42",
                "evidence_type": "context_only",
                "typed_payload": {"paid_amount": 42},
                "limitations": ("authority_backed_evidence_missing",),
            },),
            checkpoint_events=(),
            proposed_graph=(),
            accepted_graph=(),
            rejected_or_degraded_mutations=(),
            validator_results=(),
            sql_text="",
            sql_hash="",
            artifact_audit={},
            answer_text="目标日付费金额为 42。",
            final_business_summary="目标日付费金额为 42。",
            final_explanation={"explanation": "目标日付费金额为 42。"},
        )

        summary = package["sections"][0]["payload"]
        self.assertEqual(package["status"], "failed")
        self.assertEqual(package["final_answer"], "")
        self.assertEqual(summary["answer_text"], "")
        self.assertEqual(summary["final_business_summary"], "")
        self.assertEqual(
            summary["final_explanation"]["status"],
            "blocked",
        )
        self.assertNotIn("42", str(summary["final_explanation"]))
        self.assertTrue(package["evidence_verifier_block"]["reasons"])

    def test_empty_claims_with_free_text_fail_closed_and_are_scrubbed(self):
        package = build_answer_package(
            run_id="empty-claims-free-text",
            draft_claims=(),
            evidence=(),
            checkpoint_events=(),
            proposed_graph=(),
            accepted_graph=(),
            rejected_or_degraded_mutations=(),
            validator_results=(),
            sql_text="",
            sql_hash="",
            artifact_audit={},
            answer_text="unverified answer",
            final_business_summary="unverified summary",
            final_explanation={"explanation": "unverified explanation"},
            follow_up_questions=("unverified follow-up",),
        )

        verifier = package["admin_audit"]["verifier"]
        self.assertEqual(verifier["status"], "failed")
        self.assertIn(
            "free_text_without_verified_claim",
            {item["code"] for item in verifier["errors"]},
        )
        self.assertEqual(package["final_answer"], "")
        self.assertEqual(package["follow_up_questions"], [])
        self.assertEqual(package["final_explanation"]["status"], "blocked")

    def test_hard_failure_scrubs_every_business_visible_prose_field(self):
        package = scrub_answer_package_for_delivery(
            {
                "status": "draft",
                "final_answer": "secret final",
                "answer_text": "secret answer",
                "final_business_summary": "secret summary",
                "follow_up_questions": ["secret follow-up"],
                "semantic_audit": {
                    "summary": "secret semantic",
                    "warnings": [
                        {"code": "wording_risk", "detail": "secret detail"}
                    ],
                },
                "final_explanation": {"explanation": "secret explanation"},
                "quality_gate": {
                    "business_audit_summary": "secret audit",
                    "retry_instruction": "secret retry",
                    "review_notes": "secret notes",
                    "business_insight_present": True,
                },
                "sections": [
                    {
                        "section_id": "summary",
                        "visibility": "business_summary",
                        "payload": {
                            "answer_text": "secret section answer",
                            "final_business_summary": "secret section summary",
                            "limitations": ["secret"],
                            "claims": [{"text": "secret claim"}],
                            "claim_groups": [{"title": "secret group"}],
                            "visualization_plan": {"title": "secret chart"},
                            "final_explanation": {"text": "secret section explanation"},
                        },
                    }
                ],
                "admin_audit": {
                    "verifier": {
                        "status": "failed",
                        "errors": [{"code": "authority_failed", "detail": "secret"}],
                        "rejected_claim_indexes": [],
                    },
                    "original": "secret retained for admin",
                },
            }
        )

        self.assertEqual(package["status"], "failed")
        self.assertEqual(package["final_answer"], "")
        self.assertEqual(package["answer_text"], "")
        self.assertEqual(package["final_business_summary"], "")
        self.assertEqual(package["follow_up_questions"], [])
        self.assertEqual(
            package["semantic_audit"],
            {"warnings": [{"code": "wording_risk"}]},
        )
        self.assertEqual(package["quality_gate"]["status"], "failed")
        self.assertEqual(package["quality_gate"]["code"], "evidence_verifier_failed")
        self.assertFalse(package["quality_gate"]["has_verified_claims"])
        self.assertNotIn("business_audit_summary", package["quality_gate"])
        self.assertNotIn("retry_instruction", package["quality_gate"])
        self.assertNotIn("review_notes", package["quality_gate"])
        summary = package["sections"][0]["payload"]
        self.assertEqual(summary["limitations"], [])
        self.assertNotIn("secret", str({
            key: value for key, value in package.items() if key != "admin_audit"
        }))
        self.assertNotIn("original", package["admin_audit"])

    def test_passed_with_warnings_remains_deliverable(self):
        package = scrub_answer_package_for_delivery(
            {
                "status": "draft",
                "final_answer": "verified answer",
                "follow_up_questions": ["verified follow-up"],
                "quality_gate": {"business_audit_summary": "risk only"},
                "sections": [],
                "admin_audit": {
                    "verifier": {
                        "status": "passed_with_warnings",
                        "warnings": [{"code": "wording_risk"}],
                        "rejected_claim_indexes": [],
                    }
                },
            }
        )

        self.assertEqual(package["status"], "draft")
        self.assertEqual(package["final_answer"], "verified answer")
        self.assertNotIn("evidence_verifier_block", package)

    def test_available_fields_for_contract_diagnostics_ignores_projected_rows(self):
        fields = _available_fields_for_contract_diagnostics(
            {
                "schema": {"fields": ("schema_field",)},
                "request": {
                    "available_fields": ("request_field",),
                    "rows": ({"projected_only_field": 1},),
                },
            }
        )

        self.assertEqual(fields, ("request_field", "schema_field"))

    def test_contract_gap_diagnostics_use_request_available_fields(self):
        diagnostics = _contract_gap_diagnostics_from_state(
            {
                "schema": {"fields": ()},
                "request": {
                    "available_fields": ("payment_status",),
                    "compiler_runtime_plan": {
                        "row_shapes": (
                            {
                                "contract_gaps": (
                                    {
                                        "gap_id": "payment_status_contract_missing",
                                        "fields": ("payment_status",),
                                    },
                                )
                            },
                        )
                    },
                    "contract_fields": (),
                },
            }
        )

        self.assertEqual(diagnostics[0]["status"], "contract_absent")
        self.assertEqual(diagnostics[0]["data_presence"], "field_present")

    def test_contract_gap_diagnostics_use_real_compiler_gap_descriptors(self):
        compiled = compile_graph(
            question_family="data_quality_or_evidence_review",
            target_metric="paid_amount",
            requested_nodes=("data_quality_profile", "answer_verify"),
            question_text="这个结论的数据证据够不够？是否存在支付状态缺失或重复订单影响判断？",
        )

        diagnostics = _contract_gap_diagnostics_from_state(
            {
                "request": {
                    "compiler_runtime_plan": compiled.runtime_plan,
                    "available_fields": ("payment_status",),
                    "contract_fields": (),
                }
            }
        )

        by_id = {item["gap_id"]: item for item in diagnostics}
        self.assertEqual(by_id["payment_status_contract_missing"]["status"], "contract_absent")
        self.assertEqual(by_id["duplicate_order_contract_missing"]["status"], "data_absent")
        self.assertTrue(all(item["status"] != "unknown" for item in diagnostics))

    def test_multi_family_compiler_keeps_typed_gap_descriptors_unique(self):
        compiled = compile_graph(
            question_family="business_object_impact_review",
            question_families=(
                "business_object_impact_review",
                "data_quality_or_evidence_review",
            ),
            target_metric="paid_amount",
            requested_nodes=("data_quality_profile", "event_evidence", "answer_verify"),
            question_text="复核活动事件影响，并检查支付状态和重复订单证据。",
        )

        gaps = compiled.runtime_plan["row_shapes"][0]["contract_gaps"]
        ids = [item["gap_id"] for item in gaps]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("event_context_contract_missing", ids)
        self.assertIn("payment_status_contract_missing", ids)
        self.assertIn("duplicate_order_contract_missing", ids)
        self.assertTrue(all(item.get("fields") or item.get("required_fields") for item in gaps))

    def test_answer_package_keeps_causal_audit_in_admin_audit_only(self):
        package = build_answer_package(
            run_id="causal-audit-package",
            draft_claims=[],
            evidence=[],
            checkpoint_events=[],
            proposed_graph=[],
            accepted_graph=[],
            rejected_or_degraded_mutations=[],
            validator_results=[],
            sql_text="SELECT 1",
            sql_hash="hash",
            artifact_audit={},
            causal_audit={"causal_assessment": "candidate_hypothesis"},
            causal_evidence_dossier={"target_claim": "候选机制"},
        )

        summary_payload = package["sections"][0]["payload"]
        admin_payload = package["admin_audit"]

        self.assertNotIn("causal_evidence_dossier", summary_payload)
        self.assertEqual(
            admin_payload["causal_audit"]["causal_assessment"],
            "candidate_hypothesis",
        )
        self.assertEqual(
            admin_payload["causal_evidence_dossier"]["target_claim"],
            "候选机制",
        )

    def test_answer_package_keeps_contract_gap_diagnostics_in_admin_audit(self):
        package = build_answer_package(
            run_id="contract-gap-package",
            draft_claims=[],
            evidence=[],
            checkpoint_events=[],
            proposed_graph=[],
            accepted_graph=[],
            rejected_or_degraded_mutations=[],
            validator_results=[],
            sql_text="SELECT 1",
            sql_hash="hash",
            artifact_audit={},
            contract_gap_diagnostics=(
                {
                    "gap_id": "payment_status_contract_missing",
                    "status": "contract_absent",
                    "data_presence": "field_present",
                    "contract_presence": "missing",
                    "owner": "语义合同 owner",
                    "repair_path": "补语义合同，声明口径、粒度、刷新规则和可支持 claim。",
                    "claim_effect": "degrade_claim_strength",
                },
            ),
        )

        self.assertEqual(
            package["admin_audit"]["contract_gap_diagnostics"][0]["status"],
            "contract_absent",
        )

    def test_unbound_joint_attribution_claim_has_no_published_visual(self):
        package = build_answer_package(
            run_id="joint-visual-package",
            draft_claims=[
                {
                    "text": "组合贡献拆解显示 WajeSpecial × 月初贡献最大。",
                    "evidence_refs": ["joint_attribution:inline"],
                    "scope": "all_users",
                    "time_window": "2026-01-01..2026-06-30",
                    "numbers": {},
                }
            ],
            evidence=[
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "evidence_type": "statistical_association",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "limitations": [],
                    "typed_payload": {
                        "scope": "all_users",
                        "time_window": "2026-01-01..2026-06-30",
                    },
                }
            ],
            checkpoint_events=[],
            proposed_graph=[],
            accepted_graph=["joint_attribution"],
            rejected_or_degraded_mutations=[],
            validator_results=[],
            sql_text="SELECT 1",
            sql_hash="hash",
            artifact_audit={},
        )

        summary = package["sections"][0]["payload"]
        self.assertEqual(summary["claims"], [])
        self.assertEqual(summary["visualization_plan"]["blocks"], [])
        self.assertEqual(package["admin_audit"]["verifier"]["status"], "failed")

    def test_langgraph_failure_does_not_publish_business_conclusion(self):
        result = run_pattern_workflow(
            {"force_langgraph_failure": True, "llm_client": FakeLLMClient()}
        )
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.answer_package)
        self.assertTrue(result.failure_reason)

    def test_role_visibility_hides_admin_sql_from_business_reader(self):
        artifact = {
            "sections": [
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "draft"},
                },
                {
                    "section_id": "sql",
                    "visibility": "admin_audit",
                    "payload": {"sql": "SELECT 1"},
                },
            ]
        }
        filtered = filter_artifact_for_role(artifact, "business_reader")
        self.assertEqual(
            [section["section_id"] for section in filtered["sections"]],
            ["summary"],
        )

    def test_workflow_persists_answer_package_and_key_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "test-run",
                    "llm_client": FakeLLMClient(),
                }
            )

            self.assertEqual(result.status, "draft")
            self.assertTrue(result.answer_package)
            self.assertTrue(result.artifact_path.endswith("answer_package.json"))
            with open(result.artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)

        self.assertEqual(artifact["run_id"], "test-run")
        nodes = [event["node"] for event in artifact["checkpoint_events"]]
        for node in (
            "understand_business_intent",
            "accept_analysis_route",
            "validate_runtime_binding",
            "validate_query_completeness",
            "execute_capabilities",
            "reduce_evidence",
            "synthesize_answer",
            "semantic_audit",
            "hard_verify_answer",
            "final_business_summary",
            "answer_quality_gate",
            "persist_artifact",
        ):
            self.assertIn(node, nodes)
        self.assertLess(nodes.index("synthesize_answer"), nodes.index("hard_verify_answer"))
        self.assertLess(nodes.index("hard_verify_answer"), nodes.index("final_business_summary"))
        self.assertEqual(nodes[-1], "persist_artifact")
        self.assertIn("accepted_graph", artifact)
        self.assertIn("proposed_graph", artifact)
        self.assertIn("validator_results", artifact)
        self.assertIn("llm_calls", artifact["admin_audit"])
        summary = artifact["sections"][0]["payload"]
        self.assertIn("final_business_summary", summary)
        self.assertEqual(summary["final_business_summary"], "")
        self.assertEqual(artifact["status"], "failed")

    def test_business_artifact_sections_expose_sql_hash_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "visibility",
                    "llm_client": FakeLLMClient(),
                }
            )

            business = filter_artifact_for_role(result.answer_package, "business_reader")
            admin = filter_artifact_for_role(result.answer_package, "data_owner_admin")

        self.assertIn("sql_hash", json.dumps(business))
        self.assertNotIn("SELECT", json.dumps(business))
        self.assertNotIn("validator_results", business)
        self.assertNotIn("checkpoint_events", business)
        self.assertNotIn("proposed_graph", business)
        self.assertNotIn("accepted_graph", business)
        self.assertNotIn("rejected_or_degraded_mutations", business)
        self.assertIn("SELECT", json.dumps(admin))

    def test_analyst_diagnostics_do_not_expose_admin_validator_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "analyst",
                    "llm_client": FakeLLMClient(),
                }
            )

            analyst = filter_artifact_for_role(result.answer_package, "analyst")

        diagnostics = [
            section for section in analyst["sections"] if section["section_id"] == "diagnostics"
        ][0]
        self.assertIn("sql_hash", diagnostics["payload"])
        self.assertNotIn("validator_results", diagnostics["payload"])
        self.assertNotIn("artifact_audit", diagnostics["payload"])
        self.assertNotIn("sql_text", diagnostics["payload"])
        self.assertNotIn("proposed_graph", diagnostics["payload"])
        self.assertNotIn("accepted_graph", diagnostics["payload"])
        self.assertNotIn("rejected_or_degraded_mutations", diagnostics["payload"])
        self.assertNotIn("checkpoint_events", analyst)

    def test_wording_warnings_do_not_block_phase4_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "wording",
                    "llm_client": FakeLLMClient(),
                    "draft_claims": [
                        {
                            "text": "Month-start timing caused paid amount uplift.",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            )

            admin = filter_artifact_for_role(result.answer_package, "data_owner_admin")

        self.assertEqual(result.status, "draft")
        raw_verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "Month-start timing caused paid amount uplift.",
                    "evidence_refs": ["pattern_scan:intra_period"],
                    "numbers": {"median_uplift": 0.2},
                    "scope": "full_sample",
                    "time_window": "2024-01..2026-05",
                }
            ],
            evidence=[
                {
                    "evidence_ref": "pattern_scan:intra_period",
                    "evidence_type": "statistical_association",
                    "strength": "high",
                    "wording_limit": "supported",
                    "numeric_facts": {"median_uplift": 0.2},
                    "typed_payload": {
                        "median_uplift": 0.2,
                        "scope": "full_sample",
                        "time_window": "2024-01..2026-05",
                    },
                }
            ],
            visible_limitations=[],
        )
        self.assertTrue(
            any(
                warning["code"] == "causal_wording_without_causal_evidence"
                for warning in raw_verifier["warnings"]
            )
        )
        self.assertFalse(admin["admin_audit"]["verifier"]["warnings"])

    def test_verifier_allows_negated_causal_boundary_wording(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "这仍是观察性归因，不能直接定因果。",
                    "evidence_refs": ["joint_attribution:inline"],
                    "numbers": {},
                    "scope": "all_users",
                    "time_window": "2026-01-01..2026-06-30",
                }
            ],
            evidence=[
                {
                    "evidence_ref": "joint_attribution:inline",
                    "evidence_type": "statistical_association",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "typed_payload": {
                        "scope": "all_users",
                        "time_window": "2026-01-01..2026-06-30",
                    },
                }
            ],
            visible_limitations=[],
        )

        self.assertEqual(verifier["warnings"], [])


    def test_medium_pattern_blocks_reliable_wording(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "The paid amount shows a reliable rolling pattern.",
                    "evidence_refs": ["pattern_scan:rolling"],
                    "numbers": {"median_uplift": 0.04},
                    "scope": "full_sample",
                    "time_window": "2026-01..2026-06",
                }
            ],
            evidence=[
                {
                    "evidence_ref": "pattern_scan:rolling",
                    "capability": "pattern_scan",
                    "evidence_type": "statistical_association",
                    "strength": "medium",
                    "wording_limit": "supported",
                    "numeric_facts": {"median_uplift": 0.04},
                    "typed_payload": {
                        "median_uplift": 0.04,
                        "scope": "full_sample",
                        "time_window": "2026-01..2026-06",
                        "comparable_periods": 5,
                    },
                    "limitations": [],
                }
            ],
            visible_limitations=[],
        )

        self.assertTrue(
            any(warning["code"] == "over_strong_evidence_wording" for warning in verifier["warnings"])
        )

    def test_single_period_blocks_statistical_confidence_wording(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "The uplift has high statistical confidence and appears non-random.",
                    "evidence_refs": ["pattern_scan:custom_baseline"],
                    "numbers": {"median_uplift": 0.15},
                    "scope": "full_sample",
                    "time_window": "2026-01..2026-06",
                }
            ],
            evidence=[
                {
                    "evidence_ref": "pattern_scan:custom_baseline",
                    "capability": "pattern_scan",
                    "evidence_type": "statistical_association",
                    "strength": "high",
                    "wording_limit": "supported",
                    "numeric_facts": {"median_uplift": 0.15},
                    "typed_payload": {
                        "median_uplift": 0.15,
                        "scope": "full_sample",
                        "time_window": "2026-01..2026-06",
                        "comparable_periods": 1,
                    },
                    "limitations": [],
                }
            ],
            visible_limitations=[],
        )

        self.assertTrue(
            any(warning["code"] == "single_period_confidence_wording" for warning in verifier["warnings"])
        )

    def test_business_node_wrapper_does_not_implicitly_retry_failures(self):
        technical = run_pattern_workflow(
            {
                "force_failure": {
                    "node": "execute_capabilities",
                    "failure_type": "technical",
                },
                "llm_client": FakeLLMClient(),
            }
        )
        permission = run_pattern_workflow(
            {
                "force_failure": {
                    "node": "execute_capabilities",
                    "failure_type": "permission",
                },
                "llm_client": FakeLLMClient(),
            }
        )

        self.assertEqual(technical.status, "failed")
        self.assertEqual(
            [
                event["attempt"]
                for event in technical.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            [1],
        )
        self.assertEqual(
            [
                event["attempt"]
                for event in permission.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            [1],
        )
        self.assertEqual(
            [
                event["failure_type"]
                for event in technical.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            ["technical"],
        )
        self.assertEqual(
            [
                event["failure_type"]
                for event in permission.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            ["permission"],
        )

    def test_sql_safety_failure_returns_blocked_answer_with_validator_reason(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "sql-safety-blocked-answer",
                    "llm_client": fake,
                    "sql_text": "DROP TABLE paid_order_detail",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("blocked_explanation", fake.calls)
        self.assertNotIn("data_coverage_interpretation", fake.calls)
        payload = _llm_input_payload(result.answer_package, "blocked_explanation")
        failed_validators = [
            item for item in payload["validator_results"] if not item.get("ok", True)
        ]
        self.assertEqual(failed_validators[0]["validator"], "sql_safety")
        self.assertTrue(failed_validators[0]["reason"])

    def test_blocked_validator_audit_uses_validator_boundary_over_coverage(self):
        state = {
            "run_id": "blocked-validator-audit",
            "intent": {"scope": "full_sample", "time_window": "2026-01..2026-06"},
            "validator_results": [
                {"validator": "permission", "ok": False, "reason": "当前聚合结果受权限限制，不能发布主业务结论。"}
            ],
            "coverage_interpretation": {
                "coverage_status": "blocked",
                "business_impact": "当前查询没有返回可分析数据",
                "decision_summary": "本地覆盖检查发现硬边界，不能发布主业务结论。",
                "local_block_reason": "no_rows",
            },
            "evidence": [],
            "draft_claims": [],
            "sql_hash": "hash-blocked-validator",
        }

        _ensure_blocked_boundary_audit(state)

        self.assertEqual(len(state["evidence"]), 1)
        self.assertEqual(state["draft_claims"], [])
        evidence = state["evidence"][0]
        self.assertIn(":validator", evidence["evidence_ref"])
        self.assertEqual(evidence["typed_payload"]["boundary_type"], "validator")
        self.assertNotIn("coverage_block", evidence["evidence_ref"])
        self.assertNotIn("no_rows", evidence["limitations"])

    def test_blocked_validator_audit_uses_validator_boundary_over_contract_gap(self):
        state = {
            "run_id": "blocked-validator-contract-gap-audit",
            "intent": {"scope": "full_sample", "time_window": "2026-01..2026-06"},
            "validator_results": [
                {"validator": "sql_safety", "ok": False, "reason": "当前 SQL 不满足安全要求，不能继续执行。"}
            ],
            "contract_gap_diagnostics": [
                {
                    "gap_id": "paid_amount.event_window",
                    "status": "unsupported_grain",
                    "claim_effect": "block_main_conclusion",
                    "owner": "contracts",
                    "repair_path": "contract_upgrade",
                }
            ],
            "evidence": [],
            "draft_claims": [],
            "sql_hash": "hash-blocked-validator-contract-gap",
        }

        _ensure_blocked_boundary_audit(state)

        self.assertEqual(len(state["evidence"]), 1)
        self.assertEqual(state["draft_claims"], [])
        evidence = state["evidence"][0]
        self.assertIn(":validator", evidence["evidence_ref"])
        self.assertEqual(evidence["typed_payload"]["boundary_type"], "validator")
        self.assertEqual(evidence["limitations"], ["sql_safety"])
        self.assertNotIn(":contract_gap", evidence["evidence_ref"])

    def test_local_coverage_block_reason_ignores_failed_validators(self):
        reason = _local_coverage_block_reason(
            {
                "validator_results": [
                    {"validator": "permission", "ok": False, "reason": "aggregate_only_denied"}
                ],
                "request": {
                    "run_mode": "fixture",
                    "rows": [
                        {"period": "2026-07-01", "group": "target", "amount": 100},
                    ],
                    "required_fields": ("period", "group", "amount"),
                },
            }
        )

        self.assertEqual(reason, "")

    def test_blocked_explanation_payload_receives_contract_gap_diagnostics(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "data_quality_or_evidence_review",
                },
                "analysis_route_plan": {
                    "requested_nodes": ["data_quality_profile", "answer_verify"],
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "blocked-contract-gap-diagnostics",
                    "llm_client": fake,
                    "question": "这个结论的数据证据够不够？是否存在支付状态缺失或重复订单影响判断？",
                    "available_fields": ("payment_status",),
                    "sql_text": "DROP TABLE paid_order_detail",
                }
            )

        self.assertEqual(result.status, "draft", result.failure_reason)
        payload = _llm_input_payload(result.answer_package, "blocked_explanation")
        diagnostics = {item["gap_id"]: item for item in payload["contract_gap_diagnostics"]}
        self.assertEqual(diagnostics["payment_status_contract_missing"]["status"], "contract_absent")
        self.assertEqual(diagnostics["duplicate_order_contract_missing"]["status"], "data_absent")
        self.assertEqual(
            result.answer_package["admin_audit"]["contract_gap_diagnostics"],
            payload["contract_gap_diagnostics"],
        )

    def test_blocked_contract_gap_emits_auditable_evidence_and_zero_claims(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "business_object_impact_review",
                },
                "analysis_route_plan": {
                    "requested_nodes": [
                        "data_quality_profile",
                        "compare_periods",
                        "driver_decomposition",
                        "segment_contribution",
                        "outlier_contribution",
                        "event_evidence",
                        "answer_verify",
                    ],
                },
                "blocked_explanation": {
                    "status": "blocked",
                    "explanation": "当前缺少活动事件上下文合同与数据，不能判断这些事件是否影响了付费金额。",
                    "repair_path": "补活动事件上下文合同与数据后重跑。",
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "blocked-contract-gap-audit",
                    "llm_client": fake,
                    "question": "昨天的活动、投放预算、素材更换、版本更新、支付通道、节日或外部事件，是否影响了付费金额？",
                    "available_fields": ("business_date_lagos", "paid_amount_ngn"),
                    "sql_text": "DROP TABLE paid_order_detail",
                }
            )

        summary = result.answer_package["sections"][0]["payload"]
        evidence = result.answer_package["sections"][1]["payload"]["evidence"]
        verifier = result.answer_package["admin_audit"]["verifier"]

        self.assertEqual(result.status, "draft")
        self.assertEqual(summary["claims"], [])
        self.assertTrue(evidence)
        self.assertEqual(verifier["status"], "passed")
        self.assertEqual(verifier["errors"], [])
        self.assertEqual(
            result.answer_package["admin_audit"]["contract_gap_diagnostics"][0]["gap_id"],
            "event_context_contract_missing",
        )


if __name__ == "__main__":
    unittest.main()
