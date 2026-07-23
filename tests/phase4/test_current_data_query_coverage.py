import unittest
from copy import deepcopy

from bi_agent.runtime.current_data_coverage import current_data_coverage_cases
from bi_agent.runtime.clickhouse_query_compiler import compile_clickhouse_query
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.contracts import load_contract


class CurrentDataQueryCoverageTest(unittest.TestCase):
    def test_every_supported_current_data_case_compiles_and_has_completeness_contract(
        self,
    ):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        cases = current_data_coverage_cases(registry)

        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case.case_id):
                if case.expected_state == "supported":
                    compiled = compile_clickhouse_query(
                        case.query_contract,
                        case.snapshots,
                        registry=registry,
                        release_resolver=case.release_resolver,
                    )
                    self.assertTrue(compiled.sql_text.startswith(("SELECT", "WITH")))
                    self.assertNotIn("now(", compiled.sql_text.lower())
                    self.assertEqual(
                        tuple(case.query_contract.result_shape.required_window_ids),
                        tuple(case.query_contract.window_refs),
                    )
                    self.assertTrue(
                        {
                            "execution_succeeded",
                            "snapshot_watermark",
                            "required_fields",
                            "required_windows",
                            "complete_window_days",
                            "unique_key",
                            "provider_not_truncated",
                            "overall_channel_reconciliation",
                        }
                        <= set(case.query_contract.completeness_assertions),
                    )
                    self.assertTrue(case.query_contract.result_shape.required_fields)
                    self.assertTrue(case.query_contract.result_shape.unique_key)
                    self.assertTrue(case.query_contract.result_shape.grain)
                    self.assertTrue(case.source_fields)
                    self.assertTrue(case.window_policy)
                    self.assertTrue(case.provider_bounds)
                else:
                    self.assertTrue(case.gap_type)
                    self.assertTrue(case.owner)

    def test_generated_set_closes_registered_adapters_obligations_pairs_and_windows(
        self,
    ):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        cases = current_data_coverage_cases(registry)

        covered_datasets = {
            dataset_id for case in cases for dataset_id in case.dataset_ids
        }
        registered_adapters = {
            dataset_id
            for metric_id in registry.metric_ids
            for dataset_id in registry.metric_sources(metric_id)
        }
        self.assertTrue(registered_adapters <= covered_datasets)

        obligation_families = {
            query_family
            for capability_id in registry.capability_ids
            for query_family in registry.capability_inputs(capability_id).get(
                "query_families", ()
            )
        }
        self.assertTrue(obligation_families <= {case.query_family for case in cases})

        supported_datasets = {
            case.dataset_ids[0]
            for case in cases
            if case.expected_state == "supported" and len(case.dataset_ids) == 1
        }
        self.assertTrue(
            {"market_dashboard", "market_dashboard_channel"} <= supported_datasets
        )
        self.assertTrue({"gameplay", "gameplay_channel"} <= supported_datasets)

        covered_windows = {
            window_id for case in cases for window_id in case.required_window_ids
        }
        self.assertEqual(
            covered_windows,
            {
                "target_day",
                "previous_day",
                "rolling_7_day_baseline",
                "same_weekday_last_week",
            },
        )

    def test_generated_set_exactly_covers_every_registered_metric_adapter_pair(self):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        expected = {
            (metric_id, dataset_id)
            for metric_id in registry.metric_ids
            for dataset_id in registry.metric_sources(metric_id)
        }
        cases = current_data_coverage_cases(registry)
        actual = {
            (metric_id, dataset_id)
            for case in cases
            for metric_id in case.metric_ids
            for dataset_id in case.dataset_ids
            if dataset_id in registry.metric_sources(metric_id)
        }

        self.assertEqual(actual, expected)
        self.assertEqual(
            tuple(case.case_id for case in cases),
            tuple(sorted(case.case_id for case in cases)),
        )
        self.assertEqual(
            len(cases),
            len({case.case_id for case in cases}),
        )

    def test_generated_set_closes_registered_metric_dimension_cells_and_legal_joint_sets(
        self,
    ):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        cases = current_data_coverage_cases(registry)
        expected_cells = {
            (metric_id, dataset_id, dimension_id)
            for metric_id in registry.metric_ids
            for dataset_id in registry.metric_sources(metric_id)
            for dimension_id in registry.dimension_ids
            if dataset_id in registry.dimension_sources(dimension_id)
        }
        covered_cells = {
            (metric_id, dataset_id, dimension_id)
            for case in cases
            for metric_id in case.metric_ids
            for dataset_id in case.dataset_ids
            for dimension_id in case.dimension_ids
            if len(case.dimension_ids) == 1
        }

        self.assertEqual(covered_cells, expected_cells)
        paid_dimensions = {
            dimension_id
            for dimension_id in registry.dimension_ids
            if "paid_order_success" in registry.dimension_sources(dimension_id)
        }
        paid_independent = {
            frozenset(case.dimension_ids)
            for case in cases
            if case.dataset_ids == ("paid_order_success",)
            and case.metric_ids == ("paid_amount",)
            and case.query_family == "dimension_contribution_scan"
            and case.expected_state == "supported"
        }
        paid_joint = {
            frozenset(case.dimension_ids)
            for case in cases
            if case.dataset_ids == ("paid_order_success",)
            and case.metric_ids == ("paid_amount",)
            and case.query_family == "joint_candidate_scan"
            and case.expected_state == "supported"
        }
        self.assertEqual(
            paid_independent,
            {frozenset((dimension_id,)) for dimension_id in paid_dimensions},
        )
        self.assertEqual(
            paid_joint,
            {
                frozenset(group)
                for size in range(1, len(paid_dimensions) + 1)
                for group in __import__("itertools").combinations(
                    sorted(paid_dimensions), size
                )
            },
        )

    def test_new_registered_dimension_is_generated_without_dataset_name_code(self):
        payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
        payload["datasets"]["paid_order_success"]["schema_fields"].append("campaign")
        payload["dimensions"]["campaign"] = {
            "contract_ref": "contracts/dimensions/dimensions.yaml#campaign",
            "dataset_id": "paid_order_success",
            "source_field": "campaign",
            "allowed_grains": ["day", "window_id"],
        }

        registry = RuntimeContractRegistry(payload)
        cases = current_data_coverage_cases(registry)
        paid_dimension_count = sum(
            "paid_order_success" in registry.dimension_sources(dimension_id)
            for dimension_id in registry.dimension_ids
        )

        paid_amount = [
            case
            for case in cases
            if case.metric_ids == ("paid_amount",)
            and case.dataset_ids == ("paid_order_success",)
            and case.expected_state == "supported"
        ]
        self.assertTrue(
            any(
                case.metric_ids == ("paid_amount",)
                and case.dataset_ids == ("paid_order_success",)
                and case.dimension_ids == ("campaign",)
                and case.query_family == "dimension_contribution_scan"
                and case.expected_state == "supported"
                for case in paid_amount
            )
        )
        self.assertEqual(
            len(
                {
                    frozenset(case.dimension_ids)
                    for case in paid_amount
                    if case.query_family == "dimension_contribution_scan"
                }
            ),
            paid_dimension_count,
        )
        self.assertEqual(
            len(
                {
                    frozenset(case.dimension_ids)
                    for case in paid_amount
                    if case.query_family == "joint_candidate_scan"
                }
            ),
            (2**paid_dimension_count) - 1,
        )

    def test_dimension_grain_and_query_family_legality_degrade_every_affected_set(self):
        payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
        payload["dimensions"]["channel"]["allowed_grains"] = ["day"]
        payload["dimensions"]["payment_method"]["allowed_query_families"] = [
            "dimension_contribution_scan"
        ]

        cases = current_data_coverage_cases(RuntimeContractRegistry(payload))
        paid_amount = [
            case
            for case in cases
            if case.metric_ids == ("paid_amount",)
            and case.dataset_ids == ("paid_order_success",)
            and case.dimension_ids
        ]

        channel_cases = [
            case for case in paid_amount if "channel" in case.dimension_ids
        ]
        self.assertTrue(channel_cases)
        self.assertTrue(
            all(
                case.expected_state == "degraded"
                and case.gap_type == "unsupported_grain"
                for case in channel_cases
            )
        )
        payment_joint = [
            case
            for case in paid_amount
            if case.query_family == "joint_candidate_scan"
            and "payment_method" in case.dimension_ids
            and "channel" not in case.dimension_ids
        ]
        self.assertTrue(payment_joint)
        self.assertTrue(
            all(
                case.expected_state == "degraded"
                and case.gap_type == "contract_partial"
                for case in payment_joint
            )
        )
        self.assertTrue(
            any(
                case.query_family == "dimension_contribution_scan"
                and case.dimension_ids == ("payment_method",)
                and case.expected_state == "supported"
                for case in paid_amount
            )
        )

    def test_requested_dimension_query_family_requires_reviewed_topology(self):
        for topology in (None, "cartesian"):
            with self.subTest(topology=topology):
                payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
                if topology is None:
                    payload["query_shapes"]["joint_candidate_scan"].pop(
                        "dimension_topology"
                    )
                else:
                    payload["query_shapes"]["joint_candidate_scan"][
                        "dimension_topology"
                    ] = topology

                with self.assertRaisesRegex(
                    ValueError,
                    "runtime_query_shape_dimension_topology:joint_candidate_scan",
                ):
                    RuntimeContractRegistry(payload)

    def test_dimension_schema_boundary_is_retained_as_typed_gap(self):
        payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
        payload["datasets"]["paid_order_success"]["schema_fields"].remove("channel")
        payload["datasets"]["paid_order_success"]["customer_safe_filter_fields"].remove(
            "channel"
        )
        cases = current_data_coverage_cases(RuntimeContractRegistry(payload))
        paid_amount = [
            case
            for case in cases
            if case.metric_ids == ("paid_amount",)
            and case.dataset_ids == ("paid_order_success",)
        ]

        self.assertTrue(
            any(
                case.dimension_ids == ("channel",)
                and case.gap_type == "source_schema_mismatch"
                for case in paid_amount
            )
        )

    def test_new_registered_adapter_pair_is_generated_without_case_code(self):
        payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
        payload["metrics"]["profit"]["source_adapters"] = {
            "market_dashboard_channel": {
                **payload["metrics"]["profit"],
                "contract_ref": "contracts/sources/market-dashboard.source.yaml@0.1#field_contracts.profit",
            }
        }
        payload["metrics"]["profit"]["source_adapters"]["market_dashboard_channel"].pop(
            "source_adapters", None
        )
        cases = current_data_coverage_cases(RuntimeContractRegistry(payload))

        generated = next(
            case
            for case in cases
            if case.metric_ids == ("profit",)
            and case.dataset_ids == ("market_dashboard_channel",)
        )
        self.assertEqual(generated.expected_state, "supported")
        self.assertEqual(generated.query_family, "channel_context_probe")

    def test_missing_reviewed_source_schema_field_degrades_adapter_pair(self):
        payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
        payload["datasets"]["paid_order_success"]["schema_fields"].remove(
            "paid_amount_ngn"
        )
        payload["datasets"]["paid_order_success"]["customer_safe_filter_fields"].remove(
            "paid_amount_ngn"
        )
        cases = current_data_coverage_cases(RuntimeContractRegistry(payload))
        paid_amount = next(
            case
            for case in cases
            if case.metric_ids == ("paid_amount",)
            and case.dataset_ids == ("paid_order_success",)
        )

        self.assertEqual(paid_amount.expected_state, "degraded")
        self.assertEqual(paid_amount.gap_type, "source_schema_mismatch")

    def test_source_field_policy_is_closed_and_reviewed(self):
        payload = deepcopy(load_contract(CANONICAL_RUNTIME_BINDINGS_PATH))
        payload["query_shapes"]["daily_metric_baselines"]["source_field_policy"] = (
            "invent_fields"
        )

        with self.assertRaisesRegex(
            ValueError,
            "runtime_query_shape_source_field_policy:daily_metric_baselines",
        ):
            RuntimeContractRegistry(payload)

    def test_channel_case_references_generated_overall_contract(self):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        cases = current_data_coverage_cases(registry)
        overall = next(
            case
            for case in cases
            if case.metric_ids == ("paid_amount",)
            and case.dataset_ids == ("market_dashboard",)
        )
        channel = next(
            case
            for case in cases
            if case.metric_ids == ("paid_amount",)
            and case.dataset_ids == ("market_dashboard_channel",)
        )

        binding = channel.query_contract.reconciliation_binding
        self.assertIsNotNone(binding)
        self.assertEqual(
            binding.reference_query_role_ref,
            overall.query_contract.query_role_ref,
        )
        self.assertEqual(
            binding.reference_contract_signature,
            overall.query_contract.contract_signature,
        )

    def test_paid_success_never_uses_gameplay_or_final_outcome_fields(self):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        cases = current_data_coverage_cases(registry)

        paid_cases = [
            case for case in cases if "paid_order_success" in case.dataset_ids
        ]
        self.assertTrue(paid_cases)
        for case in paid_cases:
            fields = set(case.source_fields)
            self.assertNotIn("player_bet_amount", fields)
            self.assertNotIn("payment_final_outcome", case.dataset_ids)
            self.assertFalse({"订单id", "支付状态", "支付发起时间"} & fields)


if __name__ == "__main__":
    unittest.main()
