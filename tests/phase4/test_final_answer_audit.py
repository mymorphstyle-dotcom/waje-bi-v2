import json
import unittest

from bi_agent.runtime.langgraph_workflow import (
    _apply_final_business_summary_output,
    _final_answer_audit,
    _local_final_answer_hard_blockers,
    normalize_final_answer_audit,
)
from tests.phase4.fake_llm import FakeLLMResult


class FinalAnswerAuditTest(unittest.TestCase):
    def test_audit_receives_original_final_summary_without_local_wording_rewrite(self):
        llm = _CapturingAuditLLM()
        original_summary = (
            "我对问题的理解是：你想判断活动是否影响付费金额。\n"
            "分析脉络：我检查了活动窗口和指标变化。\n"
            "关键发现：活动窗口与付费金额变化同时出现。\n"
            "最终结论：活动是付费金额变化的因果原因。\n"
            "需要注意：当前还缺少独立对照证据。"
        )
        state = _audit_state(llm)

        _apply_final_business_summary_output(state, {"summary_text": original_summary})
        _final_answer_audit(state)

        self.assertEqual(state["final_business_summary"], original_summary)
        self.assertEqual(llm.payload["final_answer"], original_summary)

    def test_audit_merges_verified_claim_and_brief_evidence_refs_in_order(self):
        llm = _CapturingAuditLLM()
        state = _audit_state(llm)
        state["draft_claims"] = [
            {
                "text": "已验证的主结论。",
                "evidence_refs": ["pattern:primary", "driver:secondary", "pattern:primary"],
            }
        ]
        state["evidence"] = [
            {
                "evidence_ref": "pattern:primary",
                "capability_id": "pattern_scan",
                "capability": "pattern_scan",
                "evidence_type": "statistical_association",
                "strength": "high",
                "wording_limit": "supported",
                "typed_payload": {"median_uplift": 0.2, "comparable_periods": 12},
                "numeric_facts": {"median_uplift": 0.2, "comparable_periods": 12},
                "limitations": ["candidate_mechanism_only"],
                "result_refs": ["result-pattern"],
                "sql_hashes": ["sql-pattern"],
            },
            {
                "evidence_ref": "driver:secondary",
                "capability_id": "driver_decomposition",
                "capability": "driver_decomposition",
                "evidence_type": "contribution_decomposition",
                "strength": "medium",
                "wording_limit": "candidate_mechanism_only",
                "typed_payload": {"unit_value_share": 0.6},
                "numeric_facts": {"unit_value_share": 0.6},
                "limitations": [],
                "result_refs": ["result-driver"],
                "sql_hashes": ["sql-driver"],
            },
            {
                "evidence_ref": "outlier:unrelated",
                "capability_id": "outlier_contribution",
                "capability": "outlier_contribution",
                "evidence_type": "sensitivity_analysis",
                "strength": "high",
                "wording_limit": "supported",
                "typed_payload": {"outlier_share": 0.9},
                "numeric_facts": {"outlier_share": 0.9},
                "limitations": [],
                "result_refs": ["result-outlier"],
                "sql_hashes": ["sql-outlier"],
            },
        ]
        state["evidence_brief"] = {
            "evidence_refs": ["driver:secondary", "pattern:primary", "outlier:unrelated"]
        }

        _final_answer_audit(state)

        envelopes = llm.payload["evidence_envelopes"]
        self.assertEqual(
            [item["evidence_ref"] for item in envelopes],
            ["pattern:primary", "driver:secondary", "outlier:unrelated"],
        )
        self.assertEqual(envelopes[0]["evidence_type"], "statistical_association")
        self.assertEqual(envelopes[0]["strength"], "high")
        self.assertEqual(envelopes[0]["wording_limit"], "supported")
        self.assertEqual(envelopes[0]["typed_payload"]["median_uplift"], 0.2)
        self.assertEqual(envelopes[0]["numeric_facts"]["comparable_periods"], 12)
        self.assertEqual(envelopes[0]["result_refs"], ["result-pattern"])
        self.assertEqual(envelopes[0]["sql_hashes"], ["sql-pattern"])

    def test_audit_uses_evidence_brief_refs_when_claims_have_no_refs(self):
        llm = _CapturingAuditLLM()
        state = _audit_state(llm)
        state["evidence_brief"] = {"evidence_refs": ["pattern:primary"]}
        state["evidence"] = [
            {
                "evidence_ref": "pattern:primary",
                "capability_id": "pattern_scan",
                "evidence_type": "statistical_association",
                "strength": "high",
                "wording_limit": "supported",
                "typed_payload": {"median_uplift": 0.2},
                "numeric_facts": {"median_uplift": 0.2},
                "limitations": [],
                "result_refs": ["result-pattern"],
                "sql_hashes": ["sql-pattern"],
            }
        ]

        _final_answer_audit(state)

        self.assertEqual(
            [item["evidence_ref"] for item in llm.payload["evidence_envelopes"]],
            ["pattern:primary"],
        )

    def test_audit_excludes_rejected_claim_refs_while_using_brief_evidence(self):
        llm = _CapturingAuditLLM()
        state = _audit_state(llm)
        state["verifier"] = {"errors": [{"code": "number_mismatch", "claim_index": 0}]}
        state["draft_claims"] = [
            {"text": "被拒绝的结论。", "evidence_refs": ["claim:rejected"]}
        ]
        state["evidence_brief"] = {"evidence_refs": ["brief:fallback"]}
        state["evidence"] = [
            {"evidence_ref": "claim:rejected", "typed_payload": {"amount": 999}},
            {"evidence_ref": "brief:fallback", "typed_payload": {"amount": 100}},
        ]

        _final_answer_audit(state)

        self.assertEqual(
            [item["evidence_ref"] for item in llm.payload["evidence_envelopes"]],
            ["brief:fallback"],
        )

    def test_warning_audit_does_not_block_display(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "ready_with_warnings",
                "hard_blockers": [],
                "repairable_warnings": ["missing_business_interpretation"],
                "retry_instruction": "补一句业务排查方向。",
                "business_audit_summary": "答案可展示，但洞察表达偏弱。",
            }
        )

        self.assertEqual(audit["display_status"], "ready_with_warnings")
        self.assertFalse(audit["blocks_display"])
        self.assertEqual(audit["repairable_warnings"], ["missing_business_interpretation"])

    def test_hard_blocker_blocks_display(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "hard_blocked",
                "hard_blockers": ["unsupported_main_claim"],
                "repairable_warnings": [],
                "retry_instruction": "",
                "business_audit_summary": "主结论越过证据边界。",
            }
        )

        self.assertTrue(audit["blocks_display"])
        self.assertEqual(audit["hard_blockers"], ["unsupported_main_claim"])

    def test_unknown_repairable_warning_becomes_nonblocking_contract_mismatch(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "hard_blocked",
                "hard_blockers": ["totally_unknown_blocker"],
                "repairable_warnings": [
                    "missing_business_interpretation",
                    "发现一条没有证据支持的陈述",
                ],
                "retry_instruction": "补一句业务排查方向。",
                "business_audit_summary": "有一条不受支持的审计码。",
            }
        )

        self.assertEqual(audit["display_status"], "ready_with_warnings")
        self.assertFalse(audit["blocks_display"])
        self.assertEqual(audit["hard_blockers"], [])
        self.assertEqual(
            audit["repairable_warnings"],
            ["missing_business_interpretation", "final_answer_audit_contract_mismatch"],
        )
        self.assertEqual(audit["retry_instruction"], "补一句业务排查方向。")

    def test_unknown_status_and_hard_blocker_become_contract_mismatch(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "ready_no_warnings",
                "hard_blockers": ["发现一条可能冲突的结论"],
                "repairable_warnings": [],
                "retry_instruction": "",
                "business_audit_summary": "审计输出未遵守协议。",
            }
        )

        self.assertEqual(audit["display_status"], "ready_with_warnings")
        self.assertFalse(audit["blocks_display"])
        self.assertEqual(
            audit["repairable_warnings"],
            ["final_answer_audit_contract_mismatch"],
        )

    def test_unknown_wording_warning_keeps_supported_warning_and_contract_mismatch(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "ready_with_warnings",
                "hard_blockers": [],
                "repairable_warnings": ["unsupported_wording", "unsupported_material_claim"],
                "retry_instruction": "把无证据的确定性结论改成候选判断。",
                "business_audit_summary": "主结论里有一处证据边界过强。",
            }
        )

        self.assertEqual(
            audit["repairable_warnings"],
            ["unsupported_material_claim", "final_answer_audit_contract_mismatch"],
        )

    def test_local_hard_blockers_override_llm_ready_audit(self):
        class ReadyAuditLLM:
            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                output = {
                    "display_status": "ready",
                    "hard_blockers": [],
                    "repairable_warnings": [],
                    "retry_instruction": "",
                    "business_audit_summary": "答案满足当前展示边界。",
                }
                return FakeLLMResult(
                    output,
                    {
                        "task": task,
                        "provider": "fake",
                        "model": "fake-model",
                        "prompt_version": prompt_version,
                        "response_id": "fake-final-answer-audit",
                        "messages": [dict(message) for message in messages],
                        "required_keys": list(required_keys),
                        "raw_response_content": "{}",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "finished_at": "2026-01-01T00:00:00+00:00",
                        "duration_ms": 0.0,
                        "input_hash": "input-final-answer-audit",
                        "output_hash": "output-final-answer-audit",
                        "usage": {},
                        "structured_output": output,
                    },
                )

        audit = _final_answer_audit(
            {
                "llm_client": ReadyAuditLLM(),
                "llm_calls": [],
                "request": {"question": "Q2 相比 Q1 付费金额为什么变了？"},
                "final_business_summary": "最终结论：Q2 相比 Q1 的付费金额提升 20.0%。",
                "follow_up_questions": [],
                "validator_results": [
                    {"validator": "permission", "ok": False},
                    {"validator": "sql_safety", "ok": True},
                ],
                "verifier": {"errors": [{"code": "missing_evidence_ref", "claim_index": 0}]},
                "semantic_audit": {},
                "final_summary_display_warnings": [],
                "evidence_brief": {},
                "draft_claims": [{"text": "Q2 相比 Q1 的付费金额提升 20.0%。"}],
            }
        )

        self.assertEqual(audit["display_status"], "hard_blocked")
        self.assertTrue(audit["blocks_display"])
        self.assertEqual(
            audit["hard_blockers"],
            [
                "permission_leak",
                "verifier_evidence_contradiction",
                "unsupported_main_claim",
            ],
        )

    def test_visibility_only_verifier_error_does_not_map_to_unsupported_main_claim(self):
        blockers = _local_final_answer_hard_blockers(
            {
                "verifier": {"errors": [{"code": "missing_limitation_visibility"}]},
                "draft_claims": [{"text": "Q2 相比 Q1 的付费金额提升 20.0%。"}],
            }
        )

        self.assertEqual(blockers, ["verifier_evidence_contradiction"])

    def test_evidence_and_scope_verifier_errors_map_to_unsupported_main_claim(self):
        for code in ("missing_evidence_ref", "number_mismatch", "scope_mismatch"):
            with self.subTest(code=code):
                blockers = _local_final_answer_hard_blockers(
                    {
                        "verifier": {"errors": [{"code": code}]},
                        "draft_claims": [{"text": "Q2 相比 Q1 的付费金额提升 20.0%。"}],
                    }
                )

                self.assertEqual(
                    blockers,
                    ["verifier_evidence_contradiction", "unsupported_main_claim"],
                )


class _CapturingAuditLLM:
    def __init__(self):
        self.payload = None

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.payload = _input_payload(messages)
        output = {
            "display_status": "ready_with_warnings",
            "hard_blockers": [],
            "repairable_warnings": ["unsupported_material_claim"],
            "retry_instruction": "请把因果结论改为候选解释。",
            "business_audit_summary": "最终结论需要保留证据边界。",
        }
        return FakeLLMResult(output, _audit_metadata(task, prompt_version, messages, output))


def _input_payload(messages):
    user_message = next(message for message in messages if message["role"] == "user")
    content = user_message["content"]
    start = content.index("<input_json>") + len("<input_json>")
    end = content.index("</input_json>")
    return json.loads(content[start:end].strip())


def _audit_metadata(task, prompt_version, messages, output):
    return {
        "task": task,
        "provider": "fake",
        "model": "fake-model",
        "prompt_version": prompt_version,
        "response_id": "fake-final-answer-audit",
        "messages": [dict(message) for message in messages],
        "required_keys": [],
        "raw_response_content": "{}",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "duration_ms": 0.0,
        "input_hash": "input-final-answer-audit",
        "output_hash": "output-final-answer-audit",
        "usage": {},
        "structured_output": output,
    }


def _audit_state(llm):
    return {
        "llm_client": llm,
        "llm_calls": [],
        "request": {"question": "活动是否导致付费金额变化？"},
        "intent": {
            "pattern_family": "intra_period",
            "scope": "full_sample",
            "time_window": "2026-01",
        },
        "final_business_summary": "",
        "answer_text": "",
        "follow_up_questions": [],
        "validator_results": [],
        "verifier": {"errors": []},
        "semantic_audit": {"audit_status": "passed", "issues": []},
        "final_summary_display_warnings": [],
        "evidence_brief": {},
        "draft_claims": [],
        "evidence": [],
    }


if __name__ == "__main__":
    unittest.main()
