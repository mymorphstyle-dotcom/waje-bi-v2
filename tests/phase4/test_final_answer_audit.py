import json
import unittest

from bi_agent.runtime.langgraph_workflow import (
    _apply_final_business_summary_output,
    _final_answer_audit,
    _local_final_answer_hard_blockers,
    normalize_final_answer_audit,
)
from tests.support.scripted_llm import ScriptedLLMClient


class FinalAnswerAuditTest(unittest.TestCase):
    def test_audit_receives_original_summary_in_business_only_payload(self):
        llm = _CapturingAuditLLM()
        original_summary = (
            "我对问题的理解是：你想判断活动是否影响付费金额。\n"
            "分析脉络：我检查了活动窗口和指标变化。\n"
            "关键发现：活动窗口与付费金额变化同时出现。\n"
            "最终结论：当前只能确认两者同期变化，无法确认活动造成了付费金额变化。\n"
            "需要注意：当前还缺少独立对照证据。"
        )
        state = _audit_state(llm)

        _apply_final_business_summary_output(
            state,
            {
                "summary_text": original_summary,
                "statement_bindings": [
                    {
                        "excerpt": "活动窗口与付费金额变化同时出现。",
                        "statement_class": "factor_observation",
                        "authority_keys": ["结论1"],
                    },
                    {
                        "excerpt": (
                            "当前只能确认两者同期变化，无法确认活动造成了"
                            "付费金额变化。"
                        ),
                        "statement_class": "data_boundary",
                        "authority_keys": ["原因边界"],
                    },
                ],
                "display_summary": "当前只能确认同期变化，具体原因仍缺少独立证据。",
            },
        )
        _final_answer_audit(state)

        self.assertEqual(state["final_business_summary"], original_summary)
        self.assertEqual(llm.payload["finalAnswer"], original_summary)
        self.assertEqual(
            set(llm.payload),
            {"finalAnswer", "businessContext", "displayReview"},
        )
        visible = json.dumps(llm.payload, ensure_ascii=False)
        for internal in (
            "evidence_ref",
            "capability_id",
            "sql_hashes",
            "provider_metadata",
        ):
            self.assertNotIn(internal, visible)

    def test_empty_findings_produce_local_ready_summary(self):
        audit = normalize_final_answer_audit({"material_findings": []})

        self.assertEqual(audit["display_status"], "ready")
        self.assertFalse(audit["blocks_display"])
        self.assertEqual(audit["repairable_warnings"], [])
        self.assertEqual(
            audit["display_summary"],
            "答案与当前业务证据一致，可以保留。",
        )
        self.assertNotIn("provider_display_summary", audit)

    def test_material_findings_produce_local_warning_and_retry_instruction(self):
        finding = {
            "code": "unsupported_material_claim",
            "answer_excerpt": "活动导致付费金额上涨",
            "context_anchor": {"kind": "boundary", "key": "原因边界"},
            "edit_action": "remove",
            "explanation": "现有业务证据没有验证该原因。",
        }

        audit = normalize_final_answer_audit({"material_findings": [finding]})

        self.assertEqual(audit["display_status"], "ready_with_warnings")
        self.assertFalse(audit["blocks_display"])
        self.assertEqual(
            audit["repairable_warnings"],
            ["unsupported_material_claim"],
        )
        self.assertEqual(
            audit["retry_instruction"],
            "请仅处理以下已定位表达：删除“活动导致付费金额上涨”。",
        )
        self.assertEqual(
            audit["display_summary"],
            "答案有1处表述超出当前业务证据，需要按已验证事实修正。",
        )

    def test_local_hard_blockers_override_provider_empty_findings(self):
        state = _audit_state(_CapturingAuditLLM())
        state.update(
            {
                "final_business_summary": "最终结论：Q2 相比 Q1 的付费金额提升 20.0%。",
                "validator_results": [
                    {"validator": "permission", "ok": False},
                    {"validator": "sql_safety", "ok": True},
                ],
                "verifier": {
                    "errors": [
                        {"code": "missing_evidence_ref", "claim_index": 0}
                    ]
                },
                "draft_claims": [
                    {"text": "Q2 相比 Q1 的付费金额提升 20.0%。"}
                ],
            }
        )

        audit = _final_answer_audit(state)

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
                "verifier": {
                    "errors": [{"code": "missing_limitation_visibility"}]
                },
                "draft_claims": [
                    {"text": "Q2 相比 Q1 的付费金额提升 20.0%。"}
                ],
            }
        )

        self.assertEqual(blockers, ["verifier_evidence_contradiction"])

    def test_evidence_and_scope_errors_map_to_unsupported_main_claim(self):
        for code in (
            "missing_evidence_ref",
            "missing_required_claim",
            "number_mismatch",
            "scope_mismatch",
        ):
            with self.subTest(code=code):
                blockers = _local_final_answer_hard_blockers(
                    {
                        "verifier": {"errors": [{"code": code}]},
                        "draft_claims": [
                            {"text": "Q2 相比 Q1 的付费金额提升 20.0%。"}
                        ],
                    }
                )

                self.assertEqual(
                    blockers,
                    [
                        "verifier_evidence_contradiction",
                        "unsupported_main_claim",
                    ],
                )


class _CapturingAuditLLM(ScriptedLLMClient):
    def __init__(self):
        super().__init__(
            {"final_answer_audit": {"material_findings": []}}
        )
        self.payload = None

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.payload = _input_payload(messages)
        return super().invoke_json(
            task=task,
            prompt_version=prompt_version,
            messages=messages,
            required_keys=required_keys,
        )


def _input_payload(messages):
    user_message = next(message for message in messages if message["role"] == "user")
    content = user_message["content"]
    start = content.index("<input_json>") + len("<input_json>")
    end = content.index("</input_json>")
    return json.loads(content[start:end].strip())


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
