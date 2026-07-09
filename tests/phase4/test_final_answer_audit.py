import unittest

from bi_agent.runtime.langgraph_workflow import (
    _final_answer_audit,
    _local_final_answer_hard_blockers,
    normalize_final_answer_audit,
)
from tests.phase4.fake_llm import FakeLLMResult


class FinalAnswerAuditTest(unittest.TestCase):
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

    def test_unknown_hard_blocker_code_does_not_block_display(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "hard_blocked",
                "hard_blockers": ["totally_unknown_blocker"],
                "repairable_warnings": [
                    "missing_business_interpretation",
                    "unsupported_wording",
                    "unknown_warning",
                ],
                "retry_instruction": "补一句业务排查方向。",
                "business_audit_summary": "有一条不受支持的审计码。",
            }
        )

        self.assertEqual(audit["display_status"], "ready_with_warnings")
        self.assertFalse(audit["blocks_display"])
        self.assertEqual(audit["hard_blockers"], [])
        self.assertEqual(audit["repairable_warnings"], ["missing_business_interpretation"])

    def test_unsupported_material_claim_is_the_only_supported_wording_warning(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "ready_with_warnings",
                "hard_blockers": [],
                "repairable_warnings": ["unsupported_wording", "unsupported_material_claim"],
                "retry_instruction": "把无证据的确定性结论改成候选判断。",
                "business_audit_summary": "主结论里有一处证据边界过强。",
            }
        )

        self.assertEqual(audit["repairable_warnings"], ["unsupported_material_claim"])

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


if __name__ == "__main__":
    unittest.main()
