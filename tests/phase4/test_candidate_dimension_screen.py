import unittest

from bi_agent.capabilities import candidate_dimension_screen


class CandidateDimensionScreenTest(unittest.TestCase):
    def test_complete_dimensions_keep_entrants_and_exits_in_independent_rankings(self):
        evidence = candidate_dimension_screen(
            {
                "channel": (
                    {"channel": "A", "group": "baseline", "amount": 100, "n": 20},
                    {"channel": "B", "group": "baseline", "amount": 50, "n": 15},
                    {"channel": "A", "group": "target", "amount": 120, "n": 22},
                    {"channel": "C", "group": "target", "amount": 30, "n": 12},
                ),
                "region": (
                    {"region": "Lagos", "group": "baseline", "amount": 100, "n": 18},
                    {"region": "Abuja", "group": "baseline", "amount": 50, "n": 12},
                    {"region": "Lagos", "group": "target", "amount": 130, "n": 20},
                    {"region": "Abuja", "group": "target", "amount": 20, "n": 11},
                ),
            },
            overall_by_group={"baseline": 150, "target": 150},
            complete_dimensions=("channel", "region"),
            min_sample_size=10,
            top_k=5,
            result_refs=("result:dimension-scan",),
        )

        payload = evidence.typed_payload
        profiles = {item["dimension"]: item for item in payload["dimension_profiles"]}

        self.assertEqual(evidence.capability, "candidate_dimension_screen")
        self.assertEqual(evidence.evidence_type, "statistical_association")
        self.assertEqual(evidence.wording_limit, "candidate")
        self.assertEqual(evidence.result_refs, ("result:dimension-scan",))
        interpretation = payload["interpretation_contract"]
        self.assertEqual(interpretation["analysis_role"], "auxiliary_localization")
        self.assertEqual(interpretation["causal_interpretation"], "forbidden")
        self.assertEqual(
            interpretation["formula_decomposition_relationship"],
            "co_report_only_no_shared_rank_sum_or_share",
        )

        channel = profiles["channel"]
        self.assertEqual(channel["reconciliation_status"], "passed")
        self.assertEqual(channel["top_lifts"][0]["value"], "C")
        self.assertEqual(channel["top_lifts"][0]["baseline_amount"], 0.0)
        self.assertEqual(channel["top_lifts"][0]["movement_type"], "entrant")
        self.assertEqual(channel["top_drags"][0]["value"], "B")
        self.assertEqual(channel["top_drags"][0]["target_amount"], 0.0)
        self.assertEqual(channel["top_drags"][0]["movement_type"], "exit")

        region = profiles["region"]
        self.assertEqual(region["reconciliation_status"], "passed")
        self.assertEqual(region["top_lifts"][0]["value"], "Lagos")
        self.assertEqual(region["top_drags"][0]["value"], "Abuja")

    def test_unknown_sparse_and_top_k_remainder_stay_visible(self):
        evidence = candidate_dimension_screen(
            {
                "channel": (
                    {"channel": None, "group": "baseline", "amount": 20, "n": 20},
                    {"channel": "A", "group": "baseline", "amount": 100, "n": 20},
                    {"channel": "B", "group": "baseline", "amount": 50, "n": 5},
                    {"channel": "D", "group": "baseline", "amount": 40, "n": 20},
                    {"channel": "", "group": "target", "amount": 35, "n": 20},
                    {"channel": "A", "group": "target", "amount": 140, "n": 20},
                    {"channel": "B", "group": "target", "amount": 100, "n": 5},
                    {"channel": "D", "group": "target", "amount": 10, "n": 20},
                    {"channel": "C", "group": "target", "amount": 30, "n": 20},
                ),
            },
            overall_by_group={"baseline": 210, "target": 315},
            complete_dimensions=("channel",),
            min_sample_size=10,
            top_k=1,
        )

        profile = evidence.typed_payload["dimension_profiles"][0]

        self.assertEqual(profile["reconciliation_status"], "passed")
        self.assertEqual(
            profile["unknown_bucket"],
            {
                "baseline_amount": 20.0,
                "target_amount": 35.0,
                "delta": 15.0,
            },
        )
        self.assertEqual(profile["suppressed_segment_count"], 1)
        self.assertIn("sparse_dimension_values:channel", profile["limitations"])
        self.assertEqual(len(profile["top_lifts"]), 1)
        self.assertEqual(profile["top_lifts"][0]["value"], "A")
        self.assertEqual(len(profile["top_drags"]), 1)
        self.assertEqual(profile["top_drags"][0]["value"], "D")
        self.assertEqual(profile["displayed_delta"], 10.0)
        self.assertEqual(profile["remainder_delta"], 95.0)

    def test_wildcard_member_is_audited_but_never_localized_as_a_segment(self):
        evidence = candidate_dimension_screen(
            {
                "region": (
                    {"region": "*", "group": "baseline", "amount": 900, "n": 20},
                    {"region": "Lagos", "group": "baseline", "amount": 100, "n": 20},
                    {"region": "*", "group": "target", "amount": 800, "n": 20},
                    {"region": "Lagos", "group": "target", "amount": 200, "n": 20},
                ),
            },
            overall_by_group={"baseline": 1000, "target": 1000},
            complete_dimensions=("region",),
            dimension_labels={"region": "地区"},
            min_sample_size=10,
        )

        payload = evidence.typed_payload
        profile = payload["dimension_profiles"][0]

        self.assertEqual(
            profile["unknown_bucket"],
            {"baseline_amount": 900.0, "target_amount": 800.0, "delta": -100.0},
        )
        self.assertEqual(profile["non_localizable_member_count"], 1)
        self.assertEqual(profile["leading_value"], "Lagos")
        self.assertTrue(payload["dimension_findings"])
        self.assertTrue(
            all(
                item["member"] not in {"*", "Unknown"}
                for item in payload["dimension_findings"]
            )
        )

    def test_mixed_dimension_boundaries_stay_on_their_exact_dimension(self):
        evidence = candidate_dimension_screen(
            {
                "country": (
                    {"country": "NG", "group": "baseline", "amount": 99, "n": 20},
                    {"country": "Other", "group": "baseline", "amount": 1, "n": 20},
                    {"country": "NG", "group": "target", "amount": 108.9, "n": 20},
                    {"country": "Other", "group": "target", "amount": 1.1, "n": 20},
                ),
                "channel": (
                    {"channel": "A", "group": "baseline", "amount": 80, "n": 20},
                    {"channel": "B", "group": "baseline", "amount": 20, "n": 20},
                    {"channel": "A", "group": "target", "amount": 50, "n": 20},
                    {"channel": "B", "group": "target", "amount": 50, "n": 20},
                    {"channel": "C", "group": "target", "amount": 10, "n": 5},
                ),
                "device": (
                    {"device": "iOS", "group": "baseline", "amount": 50, "n": 20},
                    {"device": "Android", "group": "baseline", "amount": 50, "n": 20},
                    {"device": "iOS", "group": "target", "amount": 70, "n": 20},
                    {"device": "Android", "group": "target", "amount": 40, "n": 20},
                ),
            },
            overall_by_group={"baseline": 100, "target": 110},
            complete_dimensions=("country", "channel", "device"),
            min_sample_size=10,
        )

        profiles = {
            item["dimension"]: item
            for item in evidence.typed_payload["dimension_profiles"]
        }
        findings = {
            item["dimension_id"]: item
            for item in evidence.typed_payload["dimension_findings"]
        }

        self.assertEqual(evidence.limitations, ())
        self.assertEqual(profiles["country"]["profile_status"], "coverage_only")
        self.assertIn(
            "near_constant_dimension:country",
            profiles["country"]["limitations"],
        )
        self.assertNotIn("country", findings)
        self.assertEqual(
            findings["channel"]["limitation_refs"],
            ("sparse_dimension_values:channel",),
        )
        self.assertEqual(findings["device"]["limitation_refs"], ())

    def test_reconciliation_failure_withholds_candidate_ranking(self):
        evidence = candidate_dimension_screen(
            {
                "channel": (
                    {"channel": "A", "group": "baseline", "amount": 100, "n": 20},
                    {"channel": "A", "group": "target", "amount": 130, "n": 20},
                ),
            },
            overall_by_group={"baseline": 100, "target": 150},
            complete_dimensions=("channel",),
        )

        profile = evidence.typed_payload["dimension_profiles"][0]

        self.assertEqual(evidence.evidence_type, "insufficient_evidence")
        self.assertEqual(evidence.wording_limit, "insufficient")
        self.assertFalse(profile["candidate_eligible"])
        self.assertEqual(profile["reconciliation_status"], "failed")
        self.assertEqual(profile["target_reconciliation_gap"], -20.0)
        self.assertEqual(profile["top_lifts"], ())
        self.assertEqual(profile["top_drags"], ())
        self.assertIn("dimension_reconciliation_failed:channel", evidence.limitations)

    def test_incomplete_window_does_not_zero_fill_unpaired_values(self):
        evidence = candidate_dimension_screen(
            {
                "channel": (
                    {"channel": "A", "group": "baseline", "amount": 100, "n": 20},
                    {"channel": "A", "group": "target", "amount": 120, "n": 20},
                    {"channel": "C", "group": "target", "amount": 30, "n": 15},
                ),
            },
            overall_by_group={"baseline": 100, "target": 150},
            complete_dimensions=(),
        )

        profile = evidence.typed_payload["dimension_profiles"][0]

        self.assertEqual(evidence.evidence_type, "insufficient_evidence")
        self.assertFalse(profile["candidate_eligible"])
        self.assertEqual(profile["reconciliation_status"], "not_checked")
        self.assertEqual(profile["segment_count"], 1)
        self.assertEqual(profile["unpaired_dimension_value_count"], 1)
        self.assertEqual(profile["top_lifts"], ())
        self.assertIn("incomplete_dimension_window:channel", profile["limitations"])
        self.assertIn("unpaired_dimension_values:channel", profile["limitations"])

    def test_reconciled_zero_movement_does_not_create_a_candidate(self):
        evidence = candidate_dimension_screen(
            {
                "channel": (
                    {"channel": "A", "group": "baseline", "amount": 100, "n": 20},
                    {"channel": "A", "group": "target", "amount": 100, "n": 20},
                ),
            },
            overall_by_group={"baseline": 100, "target": 100},
            complete_dimensions=("channel",),
        )

        profile = evidence.typed_payload["dimension_profiles"][0]

        self.assertEqual(profile["reconciliation_status"], "passed")
        self.assertFalse(profile["candidate_eligible"])
        self.assertEqual(profile["top_lifts"], ())
        self.assertEqual(profile["top_drags"], ())
        self.assertIn("no_dimension_movement:channel", profile["limitations"])
        self.assertEqual(evidence.evidence_type, "insufficient_evidence")

    def test_reconciled_exploration_material_can_continue_without_public_readout(self):
        evidence = candidate_dimension_screen(
            {
                "region": (
                    {
                        "region": "A",
                        "group": "baseline",
                        "amount": 50,
                        "paid_orders": 10,
                        "paid_users": 10,
                        "n": 20,
                    },
                    {
                        "region": "A",
                        "group": "target",
                        "amount": 60,
                        "paid_orders": 12,
                        "paid_users": 10,
                        "n": 20,
                    },
                    {
                        "region": "B",
                        "group": "baseline",
                        "amount": 50,
                        "paid_orders": 10,
                        "paid_users": 10,
                        "n": 20,
                    },
                    {
                        "region": "B",
                        "group": "target",
                        "amount": 60,
                        "paid_orders": 12,
                        "paid_users": 10,
                        "n": 20,
                    },
                ),
            },
            overall_by_group={"baseline": 100, "target": 120},
            complete_dimensions=("region",),
        )

        self.assertEqual(evidence.evidence_type, "insufficient_evidence")
        self.assertTrue(evidence.typed_payload["joint_exploration_candidates"])
        self.assertEqual(
            evidence.typed_payload["continuation_contract"],
            {
                "state": "ready",
                "purpose": "dynamic_query_derivation",
                "material_ref": "joint_exploration_candidates",
                "material_count": 1,
                "claim_support": "none",
            },
        )

    def test_localizes_segments_by_global_primary_factor_without_cross_dimension_addition(
        self,
    ):
        evidence = candidate_dimension_screen(
            {
                "channel": (
                    {
                        "channel": "A",
                        "group": "baseline",
                        "amount": 200,
                        "paid_orders": 20,
                        "paid_users": 10,
                    },
                    {
                        "channel": "B",
                        "group": "baseline",
                        "amount": 100,
                        "paid_orders": 10,
                        "paid_users": 10,
                    },
                    {
                        "channel": "A",
                        "group": "target",
                        "amount": 300,
                        "paid_orders": 25,
                        "paid_users": 10,
                    },
                    {
                        "channel": "B",
                        "group": "target",
                        "amount": 60,
                        "paid_orders": 6,
                        "paid_users": 10,
                    },
                ),
                "region": (
                    {
                        "region": "North",
                        "group": "baseline",
                        "amount": 180,
                        "paid_orders": 18,
                        "paid_users": 9,
                    },
                    {
                        "region": "South",
                        "group": "baseline",
                        "amount": 120,
                        "paid_orders": 12,
                        "paid_users": 11,
                    },
                    {
                        "region": "North",
                        "group": "target",
                        "amount": 216,
                        "paid_orders": 18,
                        "paid_users": 9,
                    },
                    {
                        "region": "South",
                        "group": "target",
                        "amount": 144,
                        "paid_orders": 12,
                        "paid_users": 11,
                    },
                ),
            },
            overall_by_group={"baseline": 300, "target": 360},
            complete_dimensions=("channel", "region"),
            dimension_labels={"channel": "渠道", "region": "地区"},
            global_primary_factor="avg_order_amount",
        )

        payload = evidence.typed_payload
        profiles = {item["dimension"]: item for item in payload["dimension_profiles"]}
        channel_a = next(
            item for item in profiles["channel"]["top_lifts"] if item["value"] == "A"
        )

        self.assertEqual(channel_a["baseline_paid_frequency"], 2.0)
        self.assertEqual(channel_a["target_paid_frequency"], 2.5)
        self.assertEqual(channel_a["baseline_avg_order_amount"], 10.0)
        self.assertEqual(channel_a["target_avg_order_amount"], 12.0)
        self.assertEqual(
            channel_a["factor_changes"]["avg_order_amount"]["delta"],
            2.0,
        )
        self.assertEqual(channel_a["amount_contribution_scope"], "within_dimension")
        self.assertEqual(payload["global_primary_factor"], "avg_order_amount")
        interpretation = payload["interpretation_contract"]
        self.assertEqual(
            interpretation["ranking_scope"],
            "cross_dimension_diagnostic_priority",
        )
        self.assertEqual(interpretation["cross_dimension_additivity"], "forbidden")
        self.assertIn(
            "delta",
            interpretation["within_dimension_additivity"]["additive_measures"],
        )
        self.assertTrue(payload["diagnostic_priorities"])
        self.assertNotIn("contribution", payload["diagnostic_priorities"][0])
        self.assertTrue(payload["joint_exploration_candidates"])
        self.assertEqual(
            payload["joint_exploration_candidates"][0]["exploration_basis"],
            "coverage_reconciled_candidate",
        )

    def test_diagnostic_priority_is_independent_from_the_display_top_k(self):
        rows = tuple(
            row
            for value in ("A", "B", "C")
            for row in (
                {
                    "channel": value,
                    "group": "baseline",
                    "amount": 100,
                    "paid_orders": 10,
                    "paid_users": 10,
                },
                {
                    "channel": value,
                    "group": "target",
                    "amount": 120,
                    "paid_orders": 10,
                    "paid_users": 10,
                },
            )
        )

        top_one = candidate_dimension_screen(
            {"channel": rows},
            overall_by_group={"baseline": 300, "target": 360},
            complete_dimensions=("channel",),
            global_primary_factor="avg_order_amount",
            top_k=1,
        ).typed_payload["dimension_profiles"][0]
        top_three = candidate_dimension_screen(
            {"channel": rows},
            overall_by_group={"baseline": 300, "target": 360},
            complete_dimensions=("channel",),
            global_primary_factor="avg_order_amount",
            top_k=3,
        ).typed_payload["dimension_profiles"][0]

        self.assertEqual(top_one["primary_factor_alignment_coverage"], 1.0)
        self.assertEqual(
            top_one["diagnostic_priority_score"],
            top_three["diagnostic_priority_score"],
        )

    def test_missing_overall_reconciliation_degrades_only_the_auxiliary_screen(self):
        evidence = candidate_dimension_screen(
            {
                "channel": (
                    {"channel": "A", "group": "baseline", "amount": 100, "n": 20},
                    {"channel": "A", "group": "target", "amount": 130, "n": 20},
                )
            },
            overall_by_group={},
            complete_dimensions=("channel",),
        )

        self.assertEqual(evidence.evidence_type, "insufficient_evidence")
        self.assertEqual(evidence.wording_limit, "insufficient")
        self.assertIn("overall_reconciliation_unavailable", evidence.limitations)
        self.assertEqual(evidence.typed_payload["ranked_dimension_candidates"], ())
        self.assertEqual(evidence.typed_payload["joint_exploration_candidates"], ())


if __name__ == "__main__":
    unittest.main()
