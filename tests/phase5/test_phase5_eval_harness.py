from types import SimpleNamespace
import tempfile
import unittest

from tools.phase5.run_phase5_eval import evaluate_boundary_case
from tools.phase4.validate_phase4 import classify_route_drift


class NeedsQuestionLLM:
    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        output = {key: None for key in required_keys}
        if task == "business_intent":
            output.update(
                {
                    "question_family": "custom_baseline_comparison",
                    "target_metric": "paid_amount",
                    "pattern_family": "custom_baseline",
                    "scope": "full_sample",
                    "time_window": "current_period",
                    "target_claim": "渠道表现对比",
                    "baseline_candidates": [],
                    "status_message": "已识别为渠道对比。",
                }
            )
        elif task == "boundary_decision":
            output.update(
                {
                    "boundary_status": "needs_question",
                    "recommended_assumption": {"metric": "日均付费金额"},
                    "clarification_questions": [
                        {
                            "question": "你想看总金额还是日均金额？",
                            "options": ["日均金额", "总金额"],
                        }
                    ],
                    "decision_summary": "指标口径会影响结论。",
                }
            )
        elif task == "clarification_question":
            output.update(
                {
                    "questions": [
                        {
                            "question": "你想看总金额还是日均金额？",
                            "options": ["日均金额", "总金额"],
                        }
                    ],
                    "recommended_assumption": {"metric": "日均付费金额"},
                    "status_message": "需要先确认指标口径。",
                }
            )
        elif task == "blocked_explanation":
            output.update(
                {
                    "status": "blocked",
                    "explanation": "指标口径会影响结论，需要先确认。",
                    "owner": "业务使用者",
                    "repair_path": "确认总金额或日均金额后继续。",
                }
            )
        elif task == "final_business_summary":
            output["summary_text"] = "需要先确认指标口径后再继续。"
        return SimpleNamespace(
            output=output,
            audit={"task": task, "output": output, "prompt_version": prompt_version},
        )


class Phase5EvalHarnessTest(unittest.TestCase):
    def test_boundary_case_reports_expected_needs_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = evaluate_boundary_case(
                {
                    "case_id": "channel_total_vs_daily_average",
                    "question": "WajeSpecial 最近几个月是不是比其他渠道好？",
                    "expected_boundary_status": "needs_question",
                    "artifact_root": tmpdir,
                },
                llm_client=NeedsQuestionLLM(),
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["actual_boundary_status"], "needs_question")

    def test_route_drift_is_observed_without_auto_guardrail(self):
        result = classify_route_drift(
            pattern_family="custom_baseline",
            accepted_graph=[
                "data_quality_profile",
                "rolling_window_compare",
                "answer_verify",
            ],
            primary_evidence_capability="rolling_window_compare",
            expected_primary_capabilities=["compare_periods"],
            eval_status="degraded",
        )

        self.assertEqual(
            result,
            {
                "route_drift_observed": True,
                "route_drift_impact": "evidence_shape",
                "guardrail_promotion": "requires_human_review",
            },
        )


if __name__ == "__main__":
    unittest.main()
