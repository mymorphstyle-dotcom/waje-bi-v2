import unittest

from bi_agent.runtime.current_data_coverage import current_data_coverage_cases
from bi_agent.runtime.clickhouse_query_compiler import compile_clickhouse_query
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


class CurrentDataQueryCoverageTest(unittest.TestCase):
    def test_every_supported_current_data_case_compiles_and_has_completeness_contract(self):
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
                        } <= set(case.query_contract.completeness_assertions),
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

    def test_generated_set_closes_registered_adapters_obligations_pairs_and_windows(self):
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

        supported_pairs = {
            tuple(case.dataset_ids)
            for case in cases
            if case.expected_state == "supported"
        }
        self.assertIn(("market_dashboard", "market_dashboard_channel"), supported_pairs)
        self.assertIn(("gameplay", "gameplay_channel"), supported_pairs)

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

    def test_paid_success_never_uses_gameplay_aliases_or_payment_attempt_fields(self):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        cases = current_data_coverage_cases(registry)

        paid_cases = [
            case for case in cases if "paid_order_success" in case.dataset_ids
        ]
        self.assertTrue(paid_cases)
        for case in paid_cases:
            fields = set(case.source_fields)
            self.assertNotIn("player_bet_amount", fields)
            self.assertNotIn("payment_attempt", case.dataset_ids)
            self.assertFalse({"订单id", "支付状态", "支付发起时间"} & fields)


if __name__ == "__main__":
    unittest.main()
