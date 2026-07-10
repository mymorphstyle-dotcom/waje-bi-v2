from datetime import datetime
import unittest

from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.analysis_contracts import query_contract_signature
from bi_agent.runtime.capability_registry import public_capability_ids
from bi_agent.runtime.compiler import SUPPORTED_CAPABILITIES, compile_graph
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.dataset_catalog import DatasetCatalog, DatasetSnapshot
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


def snapshot(dataset_id, table, watermark):
    return DatasetSnapshot(
        f"snapshot:{dataset_id}:1", dataset_id, table, watermark, f"schema:{dataset_id}",
        (
            "business_date_lagos",
            "business_date",
            "event_start_date",
            "paid_amount_ngn",
            "user_id",
            "order_id",
            "channel",
            "payment_method",
            "region",
            "device_brand",
            "gameplay",
            "is_first_payment",
            "订单id",
            "支付状态",
            "支付发起时间",
        ),
        f"contract:{dataset_id}@1", ("analyst",), "2026-06-03T00:00:00+00:00", "active",
    )


class AnalysisContractCompilerTest(unittest.TestCase):
    def test_claim_strength_taxonomy_is_ranked_and_part_of_capability_signature(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        registry = RuntimeContractRegistry(payload)
        original = registry.capability_contract_signature("compare_periods")

        self.assertEqual(registry.claim_strength_rank("observed"), 1)
        self.assertEqual(registry.maximum_claim_strength_rank("directional"), 1)
        with self.assertRaisesRegex(KeyError, "unknown_claim_strength"):
            registry.claim_strength_rank("invented_strength")

        changed = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        changed["claim_strength_taxonomy"]["version"] = "2"
        self.assertNotEqual(
            RuntimeContractRegistry(changed).capability_contract_signature(
                "compare_periods"
            ),
            original,
        )

    def test_claim_strength_taxonomy_rejects_reversed_duplicate_and_unknown_ranks(self):
        cases = {
            "reversed": lambda value: value["claim_strength_ranks"].update(
                {"observed": 3, "medium": 2}
            ),
            "duplicate": lambda value: value["claim_strength_ranks"].update(
                {"observed": 2, "medium": 2}
            ),
            "zero_layer": lambda value: value["claim_strength_ranks"].update(
                {"context_only": 1}
            ),
            "unknown": lambda value: value["maximum_strength_ranks"].update(
                {"invented_category": 1}
            ),
            "ceiling": lambda value: value["maximum_strength_ranks"].update(
                {"verifier_only": 1}
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                payload = load_contract(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                )
                mutate(payload["claim_strength_taxonomy"])
                with self.assertRaisesRegex(
                    ValueError,
                    "runtime_claim_strength_taxonomy",
                ):
                    RuntimeContractRegistry(payload)

    def test_capability_policy_drift_changes_canonical_contract_ref(self):
        payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed["capability_inputs"]["compare_periods"]["degradation_policy"] = {
            "missing_required_input": "block_claim",
            "incomplete_input": "context_only",
        }
        catalogs = DatasetCatalog(
            (snapshot("paid_order_success", "paid", "2026-07-04"),)
        )

        original = compile_analysis_contract(
            run_id="run-capability-signature-original",
            proposal={
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=catalogs,
            registry=RuntimeContractRegistry(payload),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )
        drifted = compile_analysis_contract(
            run_id="run-capability-signature-drifted",
            proposal={
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=catalogs,
            registry=RuntimeContractRegistry(changed),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertNotEqual(
            original.capability_plans[0].capability_contract_ref,
            drifted.capability_plans[0].capability_contract_ref,
        )
        self.assertNotEqual(
            original.capability_plans[0].capability_contract_signature,
            drifted.capability_plans[0].capability_contract_signature,
        )

    def test_segment_queries_are_independent_and_companions_are_validation_only(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-segment-companion",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel", "region"],
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("segment_contribution",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        dimension_queries = tuple(
            query for query in outcome.query_contracts if query.dimension_bindings
        )
        self.assertEqual(len(dimension_queries), 2)
        self.assertEqual(
            {
                tuple(binding.dimension_id for binding in query.dimension_bindings)
                for query in dimension_queries
            },
            {("channel",), ("region",)},
        )
        companion_refs = set()
        for dimension_query in dimension_queries:
            binding = dimension_query.reconciliation_binding
            self.assertIsNotNone(binding)
            companion = next(
                query
                for query in outcome.query_contracts
                if query.query_role_ref == binding.reference_query_role_ref
            )
            self.assertFalse(companion.dimension_bindings)
            self.assertEqual(
                binding.reference_contract_signature,
                companion.contract_signature,
            )
            self.assertEqual(
                dimension_query.contract_signature,
                query_contract_signature(dimension_query),
            )
            companion_refs.add(companion.query_contract_id)
        segment_slots = outcome.capability_plans[0].required_input_slots
        self.assertEqual(
            outcome.capability_plans[0].analysis_contract_ref,
            outcome.analysis_contract.analysis_contract_id,
        )
        self.assertEqual(
            outcome.capability_plans[0].supported_claim_types,
            ("segment_contribution_or_mix_shift",),
        )
        self.assertEqual(len(segment_slots), 2)
        self.assertEqual(
            {
                slot.query_contract_refs[0]
                for slot in segment_slots
            },
            {query.query_contract_id for query in dimension_queries},
        )
        self.assertEqual(
            {
                ref
                for slot in segment_slots
                for ref in slot.validation_query_contract_refs
            },
            companion_refs,
        )
        queries_by_ref = {
            query.query_contract_id: query for query in dimension_queries
        }
        for slot in segment_slots:
            self.assertEqual(len(slot.query_contract_refs), 1)
            primary = queries_by_ref[slot.query_contract_refs[0]]
            self.assertEqual(
                slot.required_fields,
                primary.result_shape.required_fields,
            )
            self.assertEqual(len(slot.validation_query_contract_refs), 1)

    def test_joint_candidate_query_keeps_reviewed_dimension_combination(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-joint-candidate",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel", "region"],
                "claim_intents": ["candidate_driver"],
            },
            accepted_capabilities=("joint_attribution",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        joint = next(
            query
            for query in outcome.query_contracts
            if query.query_intent == "joint_candidate_scan"
            and query.dimension_bindings
        )
        self.assertEqual(
            tuple(binding.dimension_id for binding in joint.dimension_bindings),
            ("channel", "region"),
        )

    def test_high_value_query_uses_reviewed_parameters_in_shared_signature(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-high-value-parameters",
            proposal={
                "target_metrics": ["paid_amount"],
                "claim_intents": ["candidate_driver"],
            },
            accepted_capabilities=("high_value_user_contribution",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        query = outcome.query_contracts[0]
        self.assertEqual(
            query.query_parameters,
            {
                "threshold_quantile": 0.95,
                "threshold_reference": "within_window_user_paid_amount",
                "aggregation_grain": (
                    "window_id",
                    "observation_key",
                    "user_id",
                ),
            },
        )
        self.assertTrue(
            {
                "high_value_threshold",
                "high_value_amount",
                "high_value_paid_users",
            }.issubset(query.result_shape.required_fields)
        )
        self.assertEqual(query.join_expectation.cardinality, "many_to_one")
        self.assertEqual(query.join_expectation.max_duplicate_keys, 0)
        self.assertEqual(query.join_expectation.max_unmatched_rows, 0)
        self.assertEqual(query.contract_signature, query_contract_signature(query))

    def test_explicit_claim_outside_capability_ceiling_is_rejected(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-claim-ceiling",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["causal_effect"],
            },
            accepted_capabilities=("compare_periods", "answer_verify"),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertEqual(outcome.analysis_contract.claim_intents, ("unbound_claim_intent",))
        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id == "claim_intent:causal_effect:unsupported"
        )
        self.assertEqual(gap.gap_type, "contract_partial")
        self.assertEqual(gap.affected_claim_types, ("causal_effect",))
        self.assertEqual(gap.owner, "contract_owner")
        self.assertEqual(
            gap.repair_options,
            ("choose_supported_claim_intent", "clarify_claim_intent"),
        )

    def test_missing_dataset_date_field_blocks_query_with_typed_gap(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        missing_date = DatasetSnapshot(
            "snapshot:paid:no-date",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:no-date",
            ("paid_amount_ngn",),
            "contract:paid@1",
            ("analyst",),
            "2026-06-03T00:00:00Z",
            "active",
        )
        outcome = compile_analysis_contract(
            run_id="run-no-date",
            proposal={"target_metrics": ["paid_amount"], "claim_intents": ["comparative_change"]},
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((missing_date,)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertIn(
            "dataset:paid_order_success:schema_missing:business_date_lagos",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )
        self.assertFalse(outcome.query_contracts)

    def test_invalid_dataset_execution_contract_is_typed_and_blocks_query(self):
        cases = {
            "missing": lambda contract: (
                contract.pop("required_fields", None),
                contract.pop("date_field", None),
            ),
            "dual_date_source": lambda contract: contract.update(
                date_expression="toDate(business_date_lagos)"
            ),
            "empty": lambda contract: contract.update(
                required_fields=[],
                date_field="",
            ),
        }
        for case_name, mutate in cases.items():
            with self.subTest(case_name=case_name):
                payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
                mutate(payload["datasets"]["paid_order_success"])
                outcome = self._compile_compare_with_registry(
                    RuntimeContractRegistry(payload)
                )

                gap = next(
                    gap
                    for gap in outcome.analysis_contract.contract_gaps
                    if gap.dataset_id == "paid_order_success"
                    and gap.owner == "contract_owner"
                )
                self.assertEqual(gap.gap_type, "contract_partial")
                self.assertEqual(
                    gap.repair_options,
                    ("complete_dataset_contract",),
                )
                self.assertEqual(
                    gap.affected_capabilities,
                    ("analysis_contract", "compare_periods"),
                )
                self.assertFalse(outcome.query_contracts)

    def test_valid_dataset_date_field_and_expression_compile(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        field_outcome = self._compile_compare_with_registry(registry)

        expression_payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        dataset_contract = expression_payload["datasets"]["paid_order_success"]
        dataset_contract.pop("date_field")
        dataset_contract["date_expression"] = "toDate(business_date_lagos)"
        expression_outcome = self._compile_compare_with_registry(
            RuntimeContractRegistry(expression_payload)
        )

        self.assertEqual(len(field_outcome.query_contracts), 1)
        self.assertEqual(len(expression_outcome.query_contracts), 1)
        for outcome in (field_outcome, expression_outcome):
            self.assertFalse(
                [
                    gap
                    for gap in outcome.analysis_contract.contract_gaps
                    if gap.dataset_id == "paid_order_success"
                    and gap.owner == "contract_owner"
                ]
            )

    def test_missing_metric_field_blocks_binding_and_query(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        missing_metric = DatasetSnapshot(
            "snapshot:paid:no-amount",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:no-amount",
            ("business_date_lagos",),
            "contract:paid@1",
            ("analyst",),
            "2026-06-03T00:00:00Z",
            "active",
        )
        outcome = compile_analysis_contract(
            run_id="run-no-metric-field",
            proposal={"target_metrics": ["paid_amount"], "claim_intents": ["comparative_change"]},
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((missing_metric,)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertIn(
            "metric:paid_amount:schema_missing:paid_amount_ngn",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )
        self.assertFalse(outcome.analysis_contract.metric_bindings)
        self.assertFalse(outcome.query_contracts)

    def test_missing_dimension_field_blocks_binding_and_segment_query(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        missing_dimension = DatasetSnapshot(
            "snapshot:paid:no-channel",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:no-channel",
            ("business_date_lagos", "paid_amount_ngn"),
            "contract:paid@1",
            ("analyst",),
            "2026-06-03T00:00:00Z",
            "active",
        )
        outcome = compile_analysis_contract(
            run_id="run-no-dimension-field",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("segment_contribution",),
            catalog=DatasetCatalog((missing_dimension,)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertIn(
            "dimension:channel:schema_missing:channel",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )
        self.assertFalse(outcome.analysis_contract.dimension_bindings)
        self.assertFalse(outcome.query_contracts)

    def test_semantic_query_signature_is_run_independent_and_input_complete(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")

        def compile_one(run_id, *, filters=(), catalog=None, selected_registry=None):
            return compile_analysis_contract(
                run_id=run_id,
                proposal={
                    "target_metrics": ["paid_amount"],
                    "claim_intents": ["comparative_change"],
                    "filters": filters,
                },
                accepted_capabilities=("compare_periods",),
                catalog=catalog or DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
                registry=selected_registry or registry,
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
                permission_scope="analyst",
            )

        first = compile_one("run-signature-a").query_contracts[0]
        second = compile_one("run-signature-b").query_contracts[0]
        filtered = compile_one(
            "run-signature-filter",
            filters=({"field": "channel", "op": "eq", "value": "A"},),
        ).query_contracts[0]
        other_snapshot = DatasetSnapshot(
            "snapshot:paid:other",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:other",
            snapshot("paid_order_success", "paid", "2026-07-04").schema_fields,
            "contract:paid@1",
            ("analyst",),
            "2026-06-03T00:00:00Z",
            "active",
        )
        snapshot_changed = compile_one(
            "run-signature-snapshot",
            catalog=DatasetCatalog((other_snapshot,)),
        ).query_contracts[0]
        changed_payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        changed_payload["metrics"]["paid_amount"]["expression"] = "sum(paid_amount_ngn) * 1"
        binding_changed = compile_one(
            "run-signature-binding",
            selected_registry=RuntimeContractRegistry(changed_payload),
        ).query_contracts[0]

        self.assertNotEqual(first.query_contract_id, second.query_contract_id)
        self.assertEqual(first.contract_signature, second.contract_signature)
        self.assertEqual(first.workload_class, "interactive_aggregate")
        self.assertNotEqual(first.contract_signature, filtered.contract_signature)
        self.assertNotEqual(first.contract_signature, snapshot_changed.contract_signature)
        self.assertNotEqual(first.contract_signature, binding_changed.contract_signature)

    def test_metric_reconciliation_tolerance_is_bound_and_signed(self):
        base_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload["metrics"]["paid_amount"][
            "reconciliation_tolerance"
        ] = 0.25

        base = self._compile_compare_with_registry(
            RuntimeContractRegistry(base_payload)
        ).query_contracts[0]
        changed = self._compile_compare_with_registry(
            RuntimeContractRegistry(changed_payload)
        ).query_contracts[0]

        self.assertEqual(base.metric_bindings[0].reconciliation_tolerance, 0.01)
        self.assertEqual(changed.metric_bindings[0].reconciliation_tolerance, 0.25)
        self.assertNotEqual(base.contract_signature, changed.contract_signature)

    def test_metric_display_policy_is_bound_and_signed(self):
        base_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload["metrics"]["paid_amount"].update(
            {
                "value_semantics": "scalar_ratio",
                "display_format": "percent",
            }
        )

        base = self._compile_compare_with_registry(
            RuntimeContractRegistry(base_payload)
        ).query_contracts[0]
        changed = self._compile_compare_with_registry(
            RuntimeContractRegistry(changed_payload)
        ).query_contracts[0]

        self.assertEqual(base.metric_bindings[0].value_semantics, "raw_scalar")
        self.assertEqual(base.metric_bindings[0].display_format, "number")
        self.assertEqual(changed.metric_bindings[0].value_semantics, "scalar_ratio")
        self.assertEqual(changed.metric_bindings[0].display_format, "percent")
        self.assertNotEqual(base.contract_signature, changed.contract_signature)

    def test_invalid_or_missing_metric_display_policy_blocks_binding(self):
        for case_id, mutate in (
            (
                "invalid_pair",
                lambda metric: metric.update(
                    {"value_semantics": "raw_scalar", "display_format": "percent"}
                ),
            ),
            ("missing", lambda metric: metric.pop("display_format")),
        ):
            payload = load_contract(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            )
            mutate(payload["metrics"]["paid_amount"])

            outcome = self._compile_compare_with_registry(
                RuntimeContractRegistry(payload)
            )

            with self.subTest(case_id=case_id):
                self.assertFalse(outcome.query_contracts)
                self.assertTrue(
                    any(
                        gap.gap_id.startswith("metric:paid_amount:")
                        for gap in outcome.analysis_contract.contract_gaps
                    )
                )

    def test_invalid_metric_reconciliation_strategy_blocks_binding(self):
        payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        payload["metrics"]["paid_amount"][
            "reconciliation_strategy"
        ] = "average_the_totals"

        outcome = self._compile_compare_with_registry(
            RuntimeContractRegistry(payload)
        )

        self.assertFalse(outcome.query_contracts)
        self.assertIn(
            "metric:paid_amount:invalid:reconciliation_strategy",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )

    def test_workload_class_participates_in_dedupe(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        common = {
            "query_families": ["workload_scan"],
            "required_metrics": ["paid_amount"],
            "minimum_readiness": {"accepted_completeness": ["complete"]},
            "degradation_policy": {"missing_required_input": "block_claim"},
            "supported_evidence_types": ["statistical_association"],
            "supported_claim_types": ["comparative_change"],
            "maximum_claim_strength": "directional",
        }
        payload["capability_inputs"]["interactive_scan"] = dict(common)
        payload["capability_inputs"]["batch_scan"] = {**common, "workload_class": "batch_aggregate"}
        payload["query_shapes"]["workload_scan"] = {
            "required_fields": ["window_id", "window_role", "observation_key"],
            "unique_key": ["window_id", "observation_key"],
            "grain": ["window_id", "observation_key"],
        }
        outcome = compile_analysis_contract(
            run_id="run-workload-dedupe",
            proposal={"target_metrics": ["paid_amount"], "claim_intents": ["comparative_change"]},
            accepted_capabilities=("interactive_scan", "batch_scan"),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=RuntimeContractRegistry(payload),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertEqual(len(outcome.query_contracts), 2)
        self.assertEqual(
            {query.workload_class for query in outcome.query_contracts},
            {"interactive_aggregate", "batch_aggregate"},
        )

    def test_unsupported_target_semantic_becomes_typed_window_gap(self):
        self._assert_window_contract_gap(
            proposal={"target_semantic": "someday"},
            expected_gap_id="window:unsupported_target_semantic:someday",
        )

    def test_unsupported_baseline_becomes_typed_window_gap(self):
        self._assert_window_contract_gap(
            proposal={"baselines": ["quarter_to_date"]},
            expected_gap_id="window:unsupported_baseline:quarter_to_date",
        )

    def test_duplicate_baseline_becomes_typed_window_gap(self):
        self._assert_window_contract_gap(
            proposal={"baselines": ["previous_day", "previous_day"]},
            expected_gap_id="window:duplicate_baseline:previous_day",
        )

    def test_payment_source_gap_has_only_payment_capability_owner(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-payment-owner",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_components": ["payment_success_rate"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods", "driver_decomposition"),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id == "dataset:payment_attempt:source_unbound"
        )
        self.assertEqual(gap.affected_capabilities, ("driver_decomposition",))

    def test_event_source_gap_has_only_event_capability_owner(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-event-owner",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_context_sources": ["internal_operation_event"],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=("compare_periods", "event_evidence"),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id == "dataset:internal_operation_event:source_unbound"
        )
        self.assertEqual(gap.affected_capabilities, ("event_evidence",))

    def test_target_only_source_gap_uses_analysis_contract_owner(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-target-owner",
            proposal={"target_metrics": ["paid_amount"]},
            accepted_capabilities=("answer_verify",),
            catalog=DatasetCatalog(()),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id == "dataset:paid_order_success:source_unbound"
        )
        self.assertEqual(gap.affected_capabilities, ("analysis_contract",))

    def test_future_permission_mismatch_is_source_unbound(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        future = DatasetSnapshot(
            "snapshot:paid:future",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:future",
            snapshot("paid_order_success", "paid", "2026-07-04").schema_fields,
            "contract:paid@1",
            ("admin",),
            "2026-06-04T00:00:00Z",
            "active",
        )
        outcome = self._compile_compare_with_catalog(DatasetCatalog((future,)))

        self.assertIn(
            "dataset:paid_order_success:source_unbound",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )

    def test_eligible_permission_mismatch_is_permission_blocked(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        eligible = DatasetSnapshot(
            "snapshot:paid:eligible",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:eligible",
            snapshot("paid_order_success", "paid", "2026-07-04").schema_fields,
            "contract:paid@1",
            ("admin",),
            "2026-06-03T00:00:00Z",
            "active",
        )
        outcome = self._compile_compare_with_catalog(DatasetCatalog((eligible,)))

        self.assertIn(
            "dataset:paid_order_success:permission_blocked",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )

    def _assert_window_contract_gap(self, *, proposal, expected_gap_id):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        merged = {
            "target_metrics": ["paid_amount"],
            "claim_intents": ["comparative_change"],
            **proposal,
        }
        outcome = compile_analysis_contract(
            run_id="run-window-gap",
            proposal=merged,
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        gap = next(
            gap for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id == expected_gap_id
        )
        self.assertEqual(gap.gap_type, "contract_partial")
        self.assertTrue(gap.requires_clarification)
        self.assertFalse(outcome.analysis_contract.resolved_windows)
        self.assertFalse(outcome.query_contracts)

    def _compile_compare_with_catalog(self, catalog):
        return compile_analysis_contract(
            run_id="run-permission-classification",
            proposal={"target_metrics": ["paid_amount"], "claim_intents": ["comparative_change"]},
            accepted_capabilities=("compare_periods",),
            catalog=catalog,
            registry=RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml"),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

    def _compile_compare_with_registry(self, registry):
        return compile_analysis_contract(
            run_id="run-dataset-contract",
            proposal={
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

    def test_daily_query_shape_preserves_observation_grain(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-shape",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "baselines": ["rolling_7_day_baseline"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        shape = outcome.query_contracts[0].result_shape
        self.assertEqual(
            shape.required_fields,
            ("window_id", "window_role", "observation_key", "paid_amount"),
        )
        self.assertEqual(shape.unique_key, ("window_id", "observation_key"))
        self.assertEqual(shape.grain, ("window_id", "observation_key"))

    def test_registry_covers_canonical_revenue_capabilities(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        self.assertTrue(set(public_capability_ids()).issubset(SUPPORTED_CAPABILITIES))

        for capability_id in sorted(SUPPORTED_CAPABILITIES):
            with self.subTest(capability_id=capability_id):
                self.assertTrue(registry.capability_inputs(capability_id))

    def test_required_window_contract_gap_and_plan_scope_are_explicit(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-window-input",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "baselines": ["previous_day"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("rolling_window_compare",),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertIn(
            "capability:rolling_window_compare:required_window:rolling_7_day_baseline:unbound",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )
        self.assertEqual(
            outcome.capability_plans[0].required_input_slots[0].required_window_ids,
            ("target_day", "rolling_7_day_baseline"),
        )

    def test_required_context_source_absence_is_a_typed_capability_gap(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-context-input",
            proposal={
                "question_families": ["business_object_impact_review"],
                "target_metrics": [],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=("event_evidence",),
            catalog=DatasetCatalog(()),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        gap_ids = {gap.gap_id for gap in outcome.analysis_contract.contract_gaps}
        self.assertIn("capability:event_evidence:required_context_source:unbound", gap_ids)
        self.assertIn("capability:event_evidence:required_query:event_context_probe:unbound", gap_ids)
        self.assertFalse(outcome.capability_plans[0].required_input_slots[0].query_contract_refs)

    def test_required_dimension_absence_does_not_compile_unsegmented_query(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-dimension-input",
            proposal={
                "question_families": ["segment_or_factor_attribution"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("segment_contribution",),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        gap_ids = {gap.gap_id for gap in outcome.analysis_contract.contract_gaps}
        self.assertIn("capability:segment_contribution:required_dimension:unbound", gap_ids)
        self.assertNotIn(
            "dimension_contribution_scan",
            {query.query_intent for query in outcome.query_contracts},
        )

    def test_shared_query_family_plans_bind_only_owned_metric_contracts(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        common = {
            "query_families": ["shared_metric_scan"],
            "minimum_readiness": {"accepted_completeness": ["complete"]},
            "degradation_policy": {"missing_required_input": "block_claim"},
            "supported_evidence_types": ["statistical_association"],
            "supported_claim_types": ["comparative_change"],
            "maximum_claim_strength": "directional",
        }
        payload["capability_inputs"]["shared_amount"] = {
            **common,
            "required_metrics": ["paid_amount"],
        }
        payload["capability_inputs"]["shared_users"] = {
            **common,
            "required_metrics": ["paid_users"],
        }
        payload["query_shapes"]["shared_metric_scan"] = {
            "required_fields": ["window_id", "window_role", "observation_key"],
            "unique_key": ["window_id", "observation_key"],
            "grain": ["window_id", "observation_key"],
        }
        outcome = compile_analysis_contract(
            run_id="run-owned-query",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("shared_amount", "shared_users"),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=RuntimeContractRegistry(payload),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        query_by_ref = {query.query_contract_id: query for query in outcome.query_contracts}
        refs_by_capability = {
            plan.capability_id: plan.required_input_slots[0].query_contract_refs
            for plan in outcome.capability_plans
        }
        self.assertEqual(len(refs_by_capability["shared_amount"]), 1)
        self.assertEqual(len(refs_by_capability["shared_users"]), 1)
        self.assertEqual(
            tuple(
                binding.metric_id
                for binding in query_by_ref[refs_by_capability["shared_amount"][0]].metric_bindings
            ),
            ("paid_amount",),
        )
        self.assertEqual(
            tuple(
                binding.metric_id
                for binding in query_by_ref[refs_by_capability["shared_users"][0]].metric_bindings
            ),
            ("paid_users",),
        )

    def test_deduplicates_logical_query_when_metric_set_order_differs(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        common = {
            "query_families": ["ordered_metric_scan"],
            "minimum_readiness": {"accepted_completeness": ["complete"]},
            "degradation_policy": {"missing_required_input": "block_claim"},
            "supported_evidence_types": ["statistical_association"],
            "supported_claim_types": ["comparative_change"],
            "maximum_claim_strength": "directional",
        }
        payload["capability_inputs"]["order_a"] = {
            **common,
            "required_metrics": ["paid_amount", "paid_users"],
        }
        payload["capability_inputs"]["order_b"] = {
            **common,
            "required_metrics": ["paid_users", "paid_amount"],
        }
        payload["query_shapes"]["ordered_metric_scan"] = {
            "required_fields": ["window_id", "window_role", "observation_key"],
            "unique_key": ["window_id", "observation_key"],
            "grain": ["window_id", "observation_key"],
        }
        outcome = compile_analysis_contract(
            run_id="run-dedupe",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("order_a", "order_b"),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid", "2026-07-04"),)),
            registry=RuntimeContractRegistry(payload),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertEqual(len(outcome.query_contracts), 1)

    def test_compiled_graph_exposes_compatibility_contract_projection(self):
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            requested_nodes=("compare_periods", "answer_verify"),
            bound_context={
                "analysis_contract": {"analysis_contract_id": "analysis:run-compat:1"},
                "query_contracts": ({"query_contract_id": "query:run-compat:1"},),
                "capability_execution_plans": ({"capability_id": "compare_periods"},),
            },
        )

        self.assertEqual(
            compiled.analysis_contract["analysis_contract_id"],
            "analysis:run-compat:1",
        )
        self.assertEqual(compiled.query_contracts[0]["query_contract_id"], "query:run-compat:1")
        self.assertEqual(
            compiled.runtime_plan["capability_execution_plans"][0]["capability_id"],
            "compare_periods",
        )

    def test_compiles_explicit_llm_proposal_without_question_keywords(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        catalog = DatasetCatalog((
            snapshot("paid_order_success", "paid_success", "2026-07-04"),
            snapshot("payment_attempt", "payment_raw", "2026-07-04"),
        ))
        outcome = compile_analysis_contract(
            run_id="run-1",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "requested_components": ["paid_users", "first_paid_users", "paid_frequency", "avg_order_amount", "payment_success_rate"],
                "requested_dimensions": [],
                "baselines": ["previous_day", "rolling_7_day_baseline", "same_weekday_last_week"],
                "claim_intents": [
                    "comparative_change",
                    "formula_component_contribution",
                ],
            },
            accepted_capabilities=("compare_periods", "driver_decomposition", "answer_verify"),
            catalog=catalog,
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertEqual(outcome.analysis_contract.resolved_windows[0].label, "2026-06-02")
        intents = {contract.query_intent for contract in outcome.query_contracts}
        self.assertIn("daily_metric_baselines", intents)
        self.assertIn("component_driver_scan", intents)
        self.assertIn("payment_success_scan", intents)
        self.assertFalse(outcome.analysis_contract.contract_gaps)

    def test_distinguishes_source_absent_from_contract_absent(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-2",
            proposal={
                "question_families": ["business_object_impact_review"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
                "baselines": ["previous_day"],
                "requested_context_sources": ["internal_operation_event"],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=("event_evidence", "answer_verify"),
            catalog=DatasetCatalog(()),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )
        self.assertIn("source_unbound", {gap.gap_type for gap in outcome.analysis_contract.contract_gaps})

    def test_omitted_claim_intents_with_stale_snapshot_returns_typed_window_gap(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-stale",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
                "baselines": ["previous_day"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid_success", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-07-10T12:00:00+01:00"),
            permission_scope="analyst",
        )

        window_gap = next(
            gap for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "window_data_unavailable"
        )
        self.assertEqual(outcome.analysis_contract.claim_intents, ("comparative_change",))
        self.assertEqual(window_gap.affected_claim_types, ("comparative_change",))

    def test_unbound_claim_intent_returns_contract_partial_gap(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-unbound-claim",
            proposal={
                "question_families": ["evidence_quality_review"],
                "target_metrics": [],
                "requested_dimensions": [],
                "baselines": [],
            },
            accepted_capabilities=("answer_verify",),
            catalog=DatasetCatalog(()),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        claim_gap = next(
            gap for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "contract_partial"
        )
        self.assertEqual(outcome.analysis_contract.claim_intents, ("unbound_claim_intent",))
        self.assertEqual(claim_gap.affected_claim_types, ("unbound_claim_intent",))


if __name__ == "__main__":
    unittest.main()
