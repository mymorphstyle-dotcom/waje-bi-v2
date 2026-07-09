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
                    "evidence_types": ["statistical_association"],
                    "strength": "high",
                    "strengths": ["high"],
                    "wording_limit": "supported",
                    "wording_limits": ["supported"],
                    "limitations": [],
                    "verifier_status": "passed",
                }
            ],
        )

    def test_claim_groups_preserve_mixed_evidence_metadata_across_refs(self):
        package = build_answer_package(
            run_id="phase5-mixed-claim-group",
            draft_claims=[
                {
                    "text": "渠道差异有信号，但机制结论仍是候选。",
                    "evidence_refs": ["compare_periods:run", "outlier_scan:inline"],
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
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
                    "typed_payload": {},
                },
                {
                    "evidence_ref": "outlier_scan:inline",
                    "capability_id": "outlier_scan",
                    "evidence_type": "contextual_evidence",
                    "strength": "medium",
                    "wording_limit": "contextual",
                    "limitations": [],
                    "typed_payload": {},
                },
            ],
            checkpoint_events=[],
            proposed_graph=[],
            accepted_graph=["compare_periods", "outlier_scan", "answer_verify"],
            rejected_or_degraded_mutations=[],
            validator_results=[],
            sql_text="SELECT 1",
            sql_hash="hash",
            artifact_audit={},
        )

        claim_group = package["sections"][0]["payload"]["claim_groups"][0]
        self.assertEqual(claim_group["evidence_type"], "statistical_association")
        self.assertEqual(
            claim_group["evidence_types"],
            ["statistical_association", "contextual_evidence"],
        )
        self.assertEqual(claim_group["strengths"], ["high", "medium"])
        self.assertEqual(claim_group["wording_limits"], ["supported", "contextual"])

    def test_answer_package_emits_visualization_plan_from_verified_claim_groups(self):
        package = build_answer_package(
            run_id="phase5-visual-plan",
            draft_claims=[
                {
                    "text": "Q2 相比 Q1 日均付费金额提升 15.0%。",
                    "evidence_refs": ["compare_periods:run"],
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
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
                    "limitations": ["no_channel_breakdown"],
                    "typed_payload": {
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

        summary = package["sections"][0]["payload"]
        visual_blocks = summary["visualization_plan"]["blocks"]

        self.assertEqual(
            visual_blocks,
            [
                {
                    "id": "visual-1",
                    "block_type": "period_comparison",
                    "title": "期间对比",
                    "claim_text": "Q2 相比 Q1 日均付费金额提升 15.0%。",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "evidence_refs": ["compare_periods:run"],
                    "limitations": ["no_channel_breakdown"],
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

    def test_verifier_accepts_business_rounding_for_ratio_claims(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "单付费用户金额贡献约65%，付费用户数贡献约35%。",
                    "evidence_refs": ["driver_decomposition:inline"],
                    "numbers": {
                        "unit_value_share": 0.654,
                        "volume_share": 0.346,
                    },
                }
            ],
            evidence=[
                {
                    "evidence_ref": "driver_decomposition:inline",
                    "typed_payload": {
                        "unit_value_share": 0.6537576498494277,
                        "volume_share": 0.3462423501505722,
                    },
                    "limitations": [],
                }
            ],
            visible_limitations=[],
        )

        self.assertEqual(verifier["status"], "passed")

    def test_verifier_rejects_material_number_drift(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "单付费用户金额贡献约70%。",
                    "evidence_refs": ["driver_decomposition:inline"],
                    "numbers": {"unit_value_share": 0.70},
                }
            ],
            evidence=[
                {
                    "evidence_ref": "driver_decomposition:inline",
                    "typed_payload": {"unit_value_share": 0.6537576498494277},
                    "limitations": [],
                }
            ],
            visible_limitations=[],
        )

        self.assertEqual(verifier["status"], "failed")
        self.assertTrue(any(error["code"] == "number_mismatch" for error in verifier["errors"]))


if __name__ == "__main__":
    unittest.main()
