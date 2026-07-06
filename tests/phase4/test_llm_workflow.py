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

    def test_unsupported_pattern_cannot_synthesize_claims(self):
        rows = []
        for month in range(1, 7):
            rows.extend(
                [
                    {"month": f"2026-{month:02d}", "phase": "start", "amount": 100},
                    {"month": f"2026-{month:02d}", "phase": "mid", "amount": 120},
                    {"month": f"2026-{month:02d}", "phase": "end", "amount": 120},
                ]
            )
        fake = FakeLLMClient({"next_action": {"next_action": "synthesize_answer"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "unsupported-pattern",
                    "llm_client": fake,
                    "rows": rows,
                    "time_window": "2026-01..2026-06",
                    "pattern_params": {"target_phase": "start", "min_periods": 6},
                }
            )

        summary = result.answer_package["sections"][0]["payload"]
        self.assertFalse(summary["claims"])
        self.assertEqual(summary["final_explanation"]["status"], "degraded")
        self.assertNotIn("answer_synthesis", fake.calls)

    def test_noninteractive_coverage_question_continues_when_validators_pass(self):
        fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "needs_question",
                    "business_impact": "Model wants confirmation.",
                    "decision_summary": "Ask in interactive mode.",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-noninteractive",
                    "llm_client": fake,
                    "allow_question_interrupt": False,
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("blocked_explanation", fake.calls)

    def test_repeated_evidence_expansion_is_capped_by_trace(self):
        fake = FakeLLMClient({"next_action": {"next_action": "scan_sibling"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "loop-capped", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        plan_routes = [
            event.get("route")
            for event in result.answer_package["checkpoint_events"]
            if event.get("node") == "decide_next_action"
        ]
        self.assertEqual(plan_routes.count("plan"), 1)
        self.assertIn("synthesize_after_loop_cap", plan_routes)

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

    def test_duplicate_llm_claims_are_deduped(self):
        duplicate = {
            "text": "The same pattern claim.",
            "evidence_refs": ["pattern_scan:intra_period"],
            "numbers": {"median_uplift": 0.2},
            "scope": "full_sample",
            "time_window": "2024-01..2026-05",
        }
        fake = FakeLLMClient(
            {"answer_synthesis": {"answer_text": "Draft.", "claims": [duplicate, duplicate]}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "dedupe-claims", "llm_client": fake}
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(len(claims), 1)

    def test_llm_side_claims_do_not_enter_verified_claim_list(self):
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": "Pattern answer with side diagnostics.",
                    "claims": [
                        {
                            "text": "Data quality is high.",
                            "evidence_refs": ["data_quality_check:inline"],
                            "numbers": {"row_count": 100},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        },
                        {
                            "text": "No outliers were detected.",
                            "evidence_refs": ["outlier_scan:inline"],
                            "numbers": {"outliers": []},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        },
                    ],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "side-claims", "llm_client": fake}
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["evidence_refs"], ["pattern_scan:intra_period"])
        self.assertNotIn("Data quality", claims[0]["text"])
        self.assertNotIn("outliers", claims[0]["text"])

    def test_single_period_answer_text_uses_bounded_wording(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "pattern_family": "custom_baseline",
                    "question_family": "pattern_explanation",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01..2026-06",
                    "target_claim": "custom baseline pattern",
                    "baseline_candidates": ["custom"],
                    "status_message": "intent",
                },
                "answer_synthesis": {
                    "answer_text": "This is a high-confidence pattern and non-random.",
                    "claims": [],
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "single-period-wording",
                    "llm_client": fake,
                    "pattern_family": "custom_baseline",
                    "time_window": "2026-01..2026-06",
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                }
            )

        answer_text = result.answer_package["sections"][0]["payload"]["answer_text"]
        self.assertNotIn("high-confidence", answer_text)
        self.assertNotIn("non-random", answer_text)
        self.assertIn("1 comparable period", answer_text)
        self.assertNotIn("custom_baseline", answer_text)

    def test_semantic_audit_revision_routes_to_repair_then_bounded_claim(self):
        fake = FakeLLMClient(
            {
                "semantic_audit": {
                    "audit_status": "needs_revision",
                    "extracted_claims": [],
                    "issues": [{"type": "duplicate_claims"}],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "semantic-repair", "llm_client": fake}
            )

        self.assertIn("answer_repair", fake.calls)
        self.assertNotIn("degraded_explanation", fake.calls)
        summary = result.answer_package["sections"][0]["payload"]
        self.assertEqual(len(summary["claims"]), 1)
        self.assertEqual(summary["claims"][0]["evidence_refs"], ["pattern_scan:intra_period"])
        self.assertEqual(result.answer_package["admin_audit"]["verifier"]["errors"], [])

    def test_causal_gap_wording_is_weakened_before_verifier(self):
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": (
                        "No event-based causes were identified to explain the pattern. "
                        "No event-based explanations are available due to insufficient evidence."
                    ),
                    "claims": [
                        {
                            "text": (
                                "No event-based causes were identified to explain the pattern. "
                                "No event-based explanations are available due to insufficient evidence."
                            ),
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "weaken-causal", "llm_client": fake}
            )

        summary = result.answer_package["sections"][0]["payload"]
        self.assertNotIn("causes", summary["answer_text"])
        self.assertNotIn("due to", summary["answer_text"])
        self.assertNotIn("causes", summary["claims"][0]["text"])
        self.assertNotIn("due to", summary["claims"][0]["text"])
        self.assertFalse(result.answer_package["admin_audit"]["verifier"]["warnings"])


if __name__ == "__main__":
    unittest.main()
