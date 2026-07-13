import tempfile
import unittest

from bi_agent.runtime.compiler import SUPPORTED_CAPABILITIES, compile_graph
from bi_agent.runtime.analysis_assets import build_dimension_scan_reuse_contract
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.models import RecipeEntry
from bi_agent.runtime.recipe_registry import load_recipe_registry
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from tests.phase4.analysis_asset_fixtures import verified_dimension_scan_asset


def _contract_gap_ids(row_shape):
    return tuple(
        gap.get("gap_id") if isinstance(gap, dict) else gap
        for gap in row_shape.get("contract_gaps", ())
    )


def _contract_gap_descriptor(row_shape, gap_id):
    return next(
        gap
        for gap in row_shape.get("contract_gaps", ())
        if isinstance(gap, dict) and gap.get("gap_id") == gap_id
    )


class RecipeRegistryAndCompilerTest(unittest.TestCase):
    def test_compiler_adds_missing_contract_obligations_from_typed_intent(self):
        compiled = compile_graph(
            question_family="segment_or_factor_attribution",
            question_families=("segment_or_factor_attribution",),
            target_metric="paid_amount",
            requested_nodes=("data_quality_profile",),
            question_text="任意不参与策略的自然语言",
            bound_context={
                "analysis_requirements": {
                    "requested_dimensions": ["channel", "game"],
                    "baselines": ["previous_day"],
                    "diagnostic_tags": ["factor_topk"],
                }
            },
        )

        self.assertTrue(
            {"segment_contribution", "joint_attribution", "answer_verify"}.issubset(
                set(compiled.mutations.accepted_graph)
            )
        )
        self.assertTrue(
            any(
                record.reason in {"obligation_required", "obligation_conditional"}
                for record in compiled.mutations.records
            )
        )

    def test_all_public_question_families_compile_their_contract_obligations(self):
        runtime_registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
        for family in runtime_registry.question_family_ids:
            with self.subTest(family=family):
                compiled = compile_graph(
                    question_family=family,
                    question_families=(family,),
                    target_metric="paid_amount",
                    requested_nodes=("data_quality_profile",),
                    bound_context={"analysis_requirements": {}},
                    runtime_registry=runtime_registry,
                )
                expected = set(
                    runtime_registry.question_family_obligation(family)[
                        "required_capabilities"
                    ]
                )
                self.assertTrue(expected.issubset(compiled.mutations.accepted_graph))

    def test_all_public_families_reject_inapplicable_diagnostics_without_losing_base(self):
        runtime_registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
        diagnostic_tags = (
            "driver_focus", "change_explanation", "pattern_attribution",
            "event_impact", "revenue_health", "factor_topk", "anomaly",
            "multi_baseline", "evidence_quality",
        )
        for family in runtime_registry.question_family_ids:
            tag = next(
                candidate
                for candidate in diagnostic_tags
                if family not in runtime_registry.diagnostic_obligation(candidate)[
                    "supported_question_families"
                ]
            )
            with self.subTest(family=family, tag=tag):
                compiled = compile_graph(
                    question_family=family,
                    question_families=(family,),
                    target_metric="paid_amount",
                    requested_nodes=("data_quality_profile",),
                    bound_context={
                        "analysis_requirements": {"diagnostic_tags": [tag]}
                    },
                    runtime_registry=runtime_registry,
                )
                expected = set(
                    runtime_registry.question_family_obligation(family)[
                        "required_capabilities"
                    ]
                )
                self.assertTrue(expected.issubset(compiled.mutations.accepted_graph))
                self.assertIn(tag, compiled.mutations.rejected_or_degraded)
                self.assertTrue(
                    any(
                        record.capability == tag
                        and record.reason == "diagnostic_question_family_incompatible"
                        for record in compiled.mutations.records
                    )
                )
                diagnostic_capabilities = set(
                    runtime_registry.diagnostic_obligation(tag)[
                        "required_capabilities"
                    ]
                )
                base_capabilities = {
                    *runtime_registry.question_family_obligation(family)[
                        "required_capabilities"
                    ],
                    *runtime_registry.question_family_obligation(family)[
                        "independent_capabilities"
                    ],
                }
                self.assertTrue(
                    (diagnostic_capabilities - base_capabilities).isdisjoint(
                        compiled.mutations.accepted_graph
                    )
                )

    def test_composite_partition_compiles_base_and_applicable_diagnostics_independent_of_text(self):
        kwargs = {
            "question_family": "revenue_health_review",
            "question_families": (
                "revenue_health_review",
                "segment_or_factor_attribution",
                "anomaly_or_black_swan_review",
                "paid_amount_change_explanation",
            ),
            "target_metric": "paid_amount",
            "requested_nodes": ("data_quality_profile",),
            "bound_context": {
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "context_sources": ["internal_operation_event"],
                    "claim_intents": [
                        "formula_component_contribution",
                        "external_shock_candidate_or_anomaly",
                        "contract_coverage_and_trust_boundary",
                    ],
                    "diagnostic_tags": [
                        "revenue_health",
                        "change_explanation",
                        "driver_focus",
                        "event_impact",
                        "anomaly",
                        "evidence_quality",
                    ],
                }
            },
        }

        first = compile_graph(question_text="first wording", **kwargs)
        second = compile_graph(question_text="unrelated paraphrase", **kwargs)

        self.assertEqual(first.mutations.accepted_graph, second.mutations.accepted_graph)
        self.assertTrue(
            {
                "formula_decompose",
                "segment_breakdown",
                "segment_shift_compare",
                "outlier_scan",
                "change_point_scan",
                "driver_decomposition",
                "metric_timeseries",
                "source_reconciliation",
                "answer_verify",
            }.issubset(first.mutations.accepted_graph)
        )
        self.assertTrue(
            any(
                record.capability == "event_impact"
                and record.reason == "diagnostic_question_family_incompatible"
                for record in first.mutations.records
            )
        )
        self.assertFalse(
            any(record.reason == "obligation_conflict" for record in first.mutations.records)
        )

    def test_paraphrases_with_identical_typed_intent_compile_to_same_graph(self):
        kwargs = {
            "question_family": "paid_amount_change_explanation",
            "question_families": ("paid_amount_change_explanation",),
            "target_metric": "paid_amount",
            "requested_nodes": ("data_quality_profile",),
            "bound_context": {
                "analysis_requirements": {
                    "baselines": ["previous_day"],
                    "requested_dimensions": ["channel"],
                }
            },
        }
        first = compile_graph(question_text="昨天收入为何变化？", **kwargs)
        second = compile_graph(question_text="请解释昨日流水的变动。", **kwargs)

        self.assertEqual(
            first.mutations.accepted_graph,
            second.mutations.accepted_graph,
        )
        self.assertEqual(first.runtime_plan, second.runtime_plan)
        self.assertEqual(
            first.runtime_plan["row_shapes"],
            second.runtime_plan["row_shapes"],
        )

    def test_registry_has_eight_recipe_entries(self):
        registry = load_recipe_registry()
        self.assertEqual(
            set(registry),
            {
                "pattern_explanation",
                "paid_amount_change_explanation",
                "business_object_impact_review",
                "revenue_health_review",
                "segment_or_factor_attribution",
                "anomaly_or_black_swan_review",
                "custom_baseline_comparison",
                "data_quality_or_evidence_review",
            },
        )
        self.assertTrue(all(registry[key].subgraph_nodes for key in registry))
        for key, entry in registry.items():
            self.assertIsInstance(entry, RecipeEntry)
            self.assertEqual(entry.recipe_id, key)
            self.assertEqual(entry.question_family, key)
            self.assertTrue(set(entry.subgraph_nodes).issubset(SUPPORTED_CAPABILITIES))
            self.assertEqual(entry.default_degraded, key != "pattern_explanation")

    def test_pattern_compile_adds_required_paths_and_records_mutations(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="intra_period",
            requested_nodes=["pattern_scan"],
            registry=registry,
        )

        accepted_capabilities = {node.capability for node in compiled.accepted_nodes}
        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "data_quality_check",
                "pattern_scan",
                "formula_decompose",
                "event_evidence",
                "outlier_scan",
                "answer_verify",
            }.issubset(accepted_capabilities)
        )
        self.assertIn("segment_bridge", accepted_capabilities)
        self.assertTrue(compiled.mutations.proposed_graph)
        self.assertTrue(compiled.mutations.accepted_graph)
        self.assertTrue(
            any(item.action == "auto_added" for item in compiled.mutations.records)
        )

    def test_explicit_empty_registry_rejects_known_question_family(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            requested_nodes=["pattern_scan"],
            registry={},
        )

        self.assertEqual(compiled.status, "rejected")
        self.assertFalse(compiled.accepted_nodes)
        self.assertIn("pattern_explanation", compiled.mutations.rejected_or_degraded)
        self.assertTrue(
            any(
                item.action == "rejected"
                and item.capability == "pattern_explanation"
                and item.reason == "unknown_question_family"
                for item in compiled.mutations.records
            )
        )

    def test_pattern_compile_rejects_unknown_without_degrading_required_paths(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="intra_period",
            requested_nodes=["pattern_scan", "unsupported_magic"],
            registry=registry,
        )

        accepted_by_capability = {
            node.capability: node.status for node in compiled.accepted_nodes
        }
        self.assertEqual(compiled.status, "degraded")
        self.assertIn("unsupported_magic", compiled.mutations.rejected_or_degraded)
        self.assertTrue(
            {
                "pattern_scan": "accepted",
                "data_quality_check": "accepted",
                "formula_decompose": "accepted",
                "event_evidence": "accepted",
                "segment_bridge": "accepted",
                "outlier_scan": "accepted",
                "answer_verify": "accepted",
            }.items()
            <= accepted_by_capability.items()
        )
        self.assertTrue(
            any(
                item.action == "rejected"
                and item.capability == "unsupported_magic"
                and item.reason == "unknown_capability"
                for item in compiled.mutations.records
            )
        )

    def test_compiler_uses_registry_capabilities(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="intra_period",
            requested_nodes=("compare_period_phases", "raw_sql"),
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertIn("compare_period_phases", compiled.mutations.accepted_graph)
        self.assertIn("pattern_scan", compiled.mutations.accepted_graph)
        self.assertIn("raw_sql", compiled.mutations.rejected_or_degraded)

    def test_compiler_rejects_known_capability_for_unsupported_question_family(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="weekly",
            requested_nodes=("metric_coverage_profile", "weekday_calendar_compare"),
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertIn("weekday_calendar_compare", compiled.mutations.accepted_graph)
        self.assertNotIn("metric_coverage_profile", compiled.mutations.accepted_graph)
        self.assertIn("metric_coverage_profile", compiled.mutations.rejected_or_degraded)
        self.assertTrue(
            any(
                item.action == "rejected"
                and item.capability == "metric_coverage_profile"
                and item.reason == "unsupported_question_family"
                for item in compiled.mutations.records
            )
        )

    def test_non_pattern_recipe_compiles_as_degraded_dry_run_skeleton(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            requested_nodes=[],
            registry=registry,
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertTrue(compiled.accepted_nodes)
        self.assertTrue(all(node.status == "degraded" for node in compiled.accepted_nodes))
        self.assertTrue(
            any(
                item.action == "degraded"
                and item.reason == "non_pattern_dry_run_skeleton"
                for item in compiled.mutations.records
            )
        )

    def test_custom_baseline_comparison_accepts_harness_route(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "data_quality_profile",
                "compare_periods",
                "evidence_reduce",
                "answer_verify",
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertEqual(
            compiled.mutations.accepted_graph,
            (
                "data_quality_profile",
                "compare_periods",
                "evidence_reduce",
                "answer_verify",
                "market_health_compare",
                "user_mix_contribution",
            ),
        )
        self.assertFalse(compiled.mutations.rejected_or_degraded)

    def test_non_pattern_recipe_accepts_llm_selected_supported_nodes(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            requested_nodes=["driver_decomposition", "answer_verify"],
            registry=registry,
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {"data_quality_profile", "driver_decomposition", "answer_verify"}.issubset(
                compiled.mutations.accepted_graph
            )
        )
        self.assertFalse(compiled.mutations.rejected_or_degraded)

    def test_paid_amount_change_recipe_accepts_phase6_required_capabilities(self):
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            requested_nodes=(
                "data_quality_profile",
                "driver_decomposition",
                "answer_verify",
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertIn("driver_decomposition", compiled.mutations.accepted_graph)
        self.assertNotIn(
            "paid_amount_change_explanation",
            compiled.mutations.rejected_or_degraded,
        )

    def test_business_object_recipe_adds_contract_required_profile(self):
        compiled = compile_graph(
            question_family="business_object_impact_review",
            target_metric="paid_amount",
            requested_nodes=("answer_verify",),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertIn("data_quality_profile", compiled.mutations.accepted_graph)

    def test_composite_family_allows_secondary_family_capability(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            question_families=(
                "custom_baseline_comparison",
                "segment_or_factor_attribution",
            ),
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "data_quality_profile",
                "segment_contribution",
                "answer_verify",
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertIn("segment_contribution", compiled.mutations.accepted_graph)
        self.assertNotIn("segment_contribution", compiled.mutations.rejected_or_degraded)

    def test_custom_baseline_accepts_explicit_driver_decomposition(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            requested_nodes=["driver_decomposition", "answer_verify"],
            registry=registry,
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {"data_quality_profile", "compare_periods", "driver_decomposition", "answer_verify"}.issubset(
                compiled.mutations.accepted_graph
            )
        )
        self.assertFalse(compiled.mutations.rejected_or_degraded)

    def test_pattern_attribution_uses_typed_diagnostic_contract(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="weekly",
            requested_nodes=("pattern_scan",),
            question_text=(
                "最近付费金额是否存在固定规律，比如周末更高、月初更高、晚上更高？"
                "这个规律主要由哪个渠道、地区、用户类型或玩法带动？"
            ),
            bound_context={
                "analysis_requirements": {
                    "requested_dimensions": ["channel", "region"],
                    "diagnostic_tags": ["pattern_attribution"],
                }
            },
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "pattern_scan",
                "candidate_dimension_screen",
                "answer_verify",
            }.issubset(set(compiled.mutations.accepted_graph))
        )
        row_shape = compiled.runtime_plan["row_shapes"][0]
        self.assertEqual(row_shape["source"], "clickhouse")
        self.assertIn("channel", row_shape["dimension_keys"])
        self.assertTrue(
            any(
                record.action == "auto_added"
                and record.capability == "candidate_dimension_screen"
                and record.reason == "obligation_required"
                for record in compiled.mutations.records
            )
        )

    def test_revenue_health_compiles_registry_required_capabilities(self):
        compiled = compile_graph(
            question_family="revenue_health_review",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("data_quality_profile",),
            question_text=(
                "当前收入健康吗？是靠正常用户增长带动，还是靠少数大额用户、"
                "短期活动或异常渠道拉动？收入结构里最大的风险点是什么？"
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "data_quality_profile",
                "formula_decompose",
            }.issubset(set(compiled.mutations.accepted_graph))
        )
        row_shape = compiled.runtime_plan["row_shapes"][0]

    def test_multi_baseline_question_preserves_rolling_window_and_adds_compare(self):
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("rolling_window_compare", "driver_decomposition"),
            question_families=("paid_amount_change_explanation",),
            question_text="相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？",
            bound_context={
                "analysis_requirements": {
                    "baselines": ["previous_day", "rolling_7_day_baseline"],
                    "diagnostic_tags": ["multi_baseline"],
                }
            },
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertIn("rolling_window_compare", compiled.mutations.accepted_graph)
        self.assertIn("compare_periods", compiled.mutations.accepted_graph)
        self.assertIn("driver_decomposition", compiled.mutations.accepted_graph)
        self.assertIn("answer_verify", compiled.mutations.accepted_graph)
        self.assertIn(
            "rolling_7_day_baseline",
            compiled.runtime_plan["baselines"],
        )

    def test_compiler_uses_bound_runtime_context_before_question_text_fallback(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("compare_periods", "driver_decomposition", "answer_verify"),
            question_text="昨天付费金额为什么变化？",
            bound_context={
                "pattern_family": "custom_baseline",
                "time_window": "2026-04-01..2026-06-30",
                "baseline": {"label": "2026Q1"},
                "target": {"label": "2026Q2"},
            },
        )

        self.assertEqual(
            compiled.runtime_plan["windows"],
            {
                "target": "2026Q2",
                "baseline": "2026Q1",
                "time_window": "2026-04-01..2026-06-30",
            },
        )
        self.assertEqual(compiled.runtime_plan["baselines"], ("custom_baseline",))

    def test_compiler_passes_prior_analysis_assets_into_runtime_plan(self):
        required_fields = (
            "period",
            "group",
            "amount",
            "paid_users",
            "orders",
            "first_paid_users",
        )
        resolved_windows = {
            "target_day": {"start_inclusive": "2026-07-08", "end_exclusive": "2026-07-09", "timezone": "Africa/Lagos"},
            "previous_day": {"start_inclusive": "2026-07-07", "end_exclusive": "2026-07-08", "timezone": "Africa/Lagos"},
        }
        asset, content = verified_dimension_scan_asset(
            required_fields=required_fields,
            resolved_windows=resolved_windows,
            rows=(
                {"window_id": "previous_day", "period": "2026-07-07", "group": "baseline", "amount": 100, "paid_users": 10, "orders": 12, "first_paid_users": 3, "channel": "A"},
                {"window_id": "target_day", "period": "2026-07-08", "group": "target", "amount": 130, "paid_users": 11, "orders": 14, "first_paid_users": 4, "channel": "A"},
            ),
        )
        compiled = compile_graph(
            question_family="segment_or_factor_attribution",
            target_metric="paid_amount",
            requested_nodes=("segment_contribution", "answer_verify"),
            question_text="继续看哪个渠道影响最大",
            bound_context={
                "scope": "full_sample",
                "time_window": "2026-07-08",
                "windows": {"target": "2026-07-08", "baseline": "2026-07-07"},
                "baselines": ("previous_day",),
                "permission_scope": "analyst",
                "snapshot_version": "2026H1",
                "contract_versions": {"runtime": "contract-v1"},
                "schema_fingerprint": "schema-v1",
                "as_of": "2026-07-09T00:00:00+00:00",
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["pattern_attribution"],
                },
                **content,
            },
            prior_analysis_assets=(asset,),
        )

        self.assertIn("asset_inputs_used", compiled.runtime_plan)
        self.assertIn("asset_reuse_contract", compiled.runtime_plan)

    def test_evidence_quality_question_compiles_typed_audit_obligation(self):
        compiled = compile_graph(
            question_family="data_quality_or_evidence_review",
            target_metric="paid_amount",
            requested_nodes=("data_quality_profile", "answer_verify"),
            question_text=(
                "这个结论的数据证据够不够？是否存在数据延迟、渠道归因异常、"
                "支付状态缺失、重复订单或异常用户影响判断？"
            ),
            bound_context={
                "analysis_requirements": {
                    "claim_intents": ["contract_coverage_and_trust_boundary"],
                    "diagnostic_tags": ["evidence_quality"],
                }
            },
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "data_quality_profile",
                "metric_coverage_profile",
                "answer_verify",
            }.issubset(set(compiled.mutations.accepted_graph))
        )
        row_shape = compiled.runtime_plan["row_shapes"][0]
        self.assertIn("payment_status_contract_missing", _contract_gap_ids(row_shape))
        self.assertIn("duplicate_order_contract_missing", _contract_gap_ids(row_shape))
        self.assertEqual(
            _contract_gap_descriptor(row_shape, "payment_status_contract_missing")[
                "fields"
            ],
            ("payment_status", "pay_status", "status"),
        )

    def test_compiler_keeps_llm_requested_nodes_when_adding_supporting_nodes(self):
        compiled = compile_graph(
            question_family="segment_or_factor_attribution",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("joint_attribution", "answer_verify"),
            question_text="Q2 比 Q1 是用户数还是客单价驱动？",
            bound_context={
                "analysis_requirements": {
                    "claim_intents": ["formula_component_contribution"]
                }
            },
        )

        self.assertIn("joint_attribution", compiled.mutations.accepted_graph)
        self.assertIn("driver_decomposition", compiled.mutations.accepted_graph)
        self.assertIn("answer_verify", compiled.mutations.accepted_graph)

    def test_contract_loader_rejects_non_mapping_yaml(self):
        for contents in ("- unsupported\n", "false\n", "0\n", "[]\n"):
            with self.subTest(contents=contents):
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = f"{tmpdir}/bad.yaml"
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(contents)

                    with self.assertRaisesRegex(ValueError, path):
                        load_contract(path)
