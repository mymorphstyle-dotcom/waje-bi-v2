import unittest

from bi_agent.runtime.answer_package import build_answer_package, verify_answer_package


class AnswerPackageClaimGroupsTest(unittest.TestCase):
    def test_answer_package_emits_claim_groups_with_evidence_boundary(self):
        package = build_answer_package(
            run_id="phase5-claim-group",
            draft_claims=[
                {
                    "text": "Q2 相比 Q1 日均付费金额提升 15.0%。",
                    "evidence_refs": ["compare_periods:run"],
                    "numbers": {"median_uplift": 0.1504749251624582},
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                    "target_metric": "paid_amount",
                }
            ],
            evidence=[
                {
                    "evidence_ref": "compare_periods:run",
                    "capability_id": "compare_periods",
                    "evidence_type": "statistical_association",
                    "strength": "high",
                    "wording_limit": "supported",
                    "limitations": [],
                    "typed_payload": {
                        "median_uplift": 0.1504749251624582,
                        "scope": "full_sample",
                        "time_window": "2026-01-01..2026-06-30",
                    },
                }
            ],
            checkpoint_events=[],
            proposed_graph=[],
            accepted_graph=["compare_periods", "answer_verify"],
            rejected_or_degraded_mutations=[],
            validator_results=[],
            sql_text="SELECT 1",
            sql_hash="hash",
            artifact_audit={},
        )

        claim_groups = package["sections"][0]["payload"]["claim_groups"]

        self.assertEqual(
            claim_groups,
            [
                {
                    "text": "Q2 相比 Q1 日均付费金额提升 15.0%。",
                    "scope": "full_sample",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                    "target_metric": "paid_amount",
                    "time_window": "2026-01-01..2026-06-30",
                    "evidence_refs": ["compare_periods:run"],
                    "evidence_type": "statistical_association",
                    "strength": "high",
                    "wording_limit": "supported",
                    "limitations": [],
                    "verifier_status": "passed",
                }
            ],
        )

    def test_strong_claim_fails_when_evidence_ref_is_missing(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "该模式稳定成立。",
                    "claim_strength": "strong",
                    "evidence_refs": ["missing:evidence"],
                    "numbers": {"median_uplift": 0.2},
                }
            ],
            evidence=[],
            visible_limitations=[],
        )

        self.assertEqual(verifier["status"], "failed")
        self.assertTrue(
            any(error["code"] == "missing_evidence_ref" for error in verifier["errors"])
        )

    def test_strong_claim_fails_when_wording_limit_is_too_weak(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "该对比结论稳定成立。",
                    "claim_strength": "strong",
                    "evidence_refs": ["compare_periods:run"],
                    "numbers": {"median_uplift": 0.2},
                }
            ],
            evidence=[
                {
                    "evidence_ref": "compare_periods:run",
                    "evidence_type": "statistical_association",
                    "strength": "medium",
                    "wording_limit": "tendency",
                    "typed_payload": {"median_uplift": 0.2},
                    "limitations": [],
                }
            ],
            visible_limitations=[],
        )

        self.assertEqual(verifier["status"], "failed")
        self.assertTrue(
            any(
                error["code"] == "strong_claim_without_supported_wording"
                for error in verifier["errors"]
            )
        )


if __name__ == "__main__":
    unittest.main()
