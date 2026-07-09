import unittest

from bi_agent.runtime.langgraph_workflow import normalize_final_answer_audit


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


if __name__ == "__main__":
    unittest.main()
