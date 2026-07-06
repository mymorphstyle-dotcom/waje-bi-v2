import tempfile
import unittest

from bi_agent.runtime.langgraph_workflow import run_pattern_workflow
from bi_agent.runtime.llm_client import LLMConfigurationError, OpenAICompatibleLLMClient
from bi_agent.runtime.llm_prompts import validate_prompt_specs
from tests.phase4.fake_llm import FakeLLMClient


class LLMWorkflowTest(unittest.TestCase):
    def test_prompt_specs_are_consistent(self):
        self.assertEqual(validate_prompt_specs(), [])

    def test_missing_llm_env_fails_before_claiming_draft(self):
        with self.assertRaisesRegex(LLMConfigurationError, "missing_llm_model"):
            OpenAICompatibleLLMClient.from_env({})

    def test_successful_workflow_calls_required_llm_tasks(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "llm-flow", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        for task in (
            "business_intent",
            "boundary_decision",
            "confirm_understanding",
            "analysis_route",
            "data_coverage_interpretation",
            "next_action",
            "evidence_interpretation",
            "answer_synthesis",
            "semantic_audit",
        ):
            self.assertIn(task, fake.calls)
        self.assertEqual(
            [call["task"] for call in result.answer_package["admin_audit"]["llm_calls"]],
            fake.calls,
        )

    def test_boundary_question_without_user_choice_blocks_without_conclusion(self):
        fake = FakeLLMClient(
            {
                "boundary_decision": {
                    "boundary_status": "needs_question",
                    "recommended_assumption": {"scope": "full_sample"},
                    "clarification_questions": [
                        {
                            "question": "Which scope should be used?",
                            "options": ["full sample", "custom scope"],
                        }
                    ],
                    "decision_summary": "Scope could change the answer.",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "needs-question", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("clarification_question", fake.calls)
        self.assertIn("blocked_explanation", fake.calls)
        summary = result.answer_package["sections"][0]["payload"]
        self.assertFalse(summary["claims"])
        self.assertEqual(summary["final_explanation"]["status"], "blocked")

    def test_degrade_suggestion_does_not_drop_established_pattern_answer(self):
        fake = FakeLLMClient({"next_action": {"next_action": "degrade"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "degrade-override", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("evidence_interpretation", fake.calls)
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("degraded_explanation", fake.calls)
        routes = [
            event.get("route")
            for event in result.answer_package["checkpoint_events"]
            if event.get("node") == "decide_next_action"
        ]
        self.assertIn("degrade_overridden_to_bounded_answer", routes)

    def test_llm_claim_time_window_is_normalized_to_evidence_window(self):
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": "Draft answer with exception period.",
                    "claims": [
                        {
                            "text": "The main pattern is supported.",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2026-05",
                        }
                    ],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "claim-window-normalized",
                    "llm_client": fake,
                }
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(claims[0]["time_window"], "2024-01..2026-05")
        self.assertEqual(result.answer_package["admin_audit"]["verifier"]["errors"], [])


if __name__ == "__main__":
    unittest.main()
