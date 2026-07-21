from dataclasses import replace
import unittest

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ResolvedWindow,
    ResultShape,
    query_contract_semantic_body,
    query_contract_from_dict,
    query_contract_signature,
    stable_contract_signature,
)


class AnalysisContractsTest(unittest.TestCase):
    def test_result_envelope_serializes_external_schema_and_keeps_internal_rows(self):
        rows = ({"window_id": "target_day", "paid_amount": 120},)
        envelope = QueryResultEnvelope(
            query_contract_ref="query:run-1:daily:1",
            query_id="provider-query-1",
            query_hash="query-hash-1",
            result_ref="result:run-1:daily:1",
            execution_status="succeeded",
            rows_ref="artifact:aggregate-rows:1",
            row_count=1,
            completeness_report_ref="completeness:run-1:daily:1",
            rows=rows,
        )

        payload = envelope.to_dict()

        self.assertEqual(envelope.rows, rows)
        self.assertEqual(payload["rows_ref"], "artifact:aggregate-rows:1")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(
            payload["completeness_report_ref"], "completeness:run-1:daily:1"
        )
        self.assertNotIn("rows", payload)

    def test_contract_relationships_are_explicit_and_serialize_nested_values(self):
        window = ResolvedWindow(
            "target_day",
            "target",
            "2026-06-02",
            "2026-06-02",
            "2026-06-03",
            "Africa/Lagos",
            "daily_total",
            1,
            "2026-06-02",
        )
        metric = MetricBinding(
            "paid_amount",
            "metric:paid_amount@1",
            "paid_order_success",
            "sum(paid_amount_ngn)",
            "sum",
            ("paid_amount_ngn",),
            ("window_id",),
        )
        slot = CapabilityInputSlot(
            "daily_totals",
            ("query:run-1:daily:1",),
            True,
            ("complete",),
            ("window_id", "paid_amount"),
            ("target_day",),
        )
        plan = CapabilityExecutionPlan(
            capability_id="compare_periods",
            capability_contract_ref="capability:compare_periods@1",
            required_input_slots=(slot,),
            optional_input_slots=(),
            merge_strategy="contract_defined",
            minimum_readiness={"required_slots": "ready"},
            degradation_policy={"missing_optional": "degraded"},
            supported_evidence_types=("accounting_contribution",),
            maximum_claim_strength="medium",
        )
        analysis = AnalysisContract(
            analysis_contract_id="analysis:run-1:1",
            contract_version="1",
            question_families=("paid_amount_change_explanation",),
            target_metric_refs=("metric:paid_amount@1",),
            claim_intents=("comparative_change",),
            scope={"type": "full_sample"},
            business_timezone="Africa/Lagos",
            as_of="2026-06-03T12:00:00+01:00",
            resolved_windows=(window,),
            metric_bindings=(metric,),
            dimension_bindings=(),
            dataset_requirements=("paid_order_success",),
            capability_requirements=("capability:compare_periods@1",),
        )
        query = QueryContract(
            query_contract_id="query:run-1:daily:1",
            analysis_contract_ref=analysis.analysis_contract_id,
            query_intent="daily_metric_baselines",
            dataset_snapshot_refs=("snapshot:paid-order:1",),
            metric_bindings=(metric,),
            dimension_bindings=(),
            window_refs=("target_day",),
            resolved_windows=(window,),
            filters=(),
            result_shape=ResultShape(
                ("window_id", "paid_amount"),
                ("window_id",),
                ("window_id",),
                ("target_day",),
            ),
            completeness_assertions=("required_windows",),
            workload_class="interactive_aggregate",
            contract_signature="signature",
        )

        analysis_payload = analysis.to_dict()
        query_payload = query.to_dict()

        self.assertEqual(
            analysis_payload["capability_requirements"],
            ("capability:compare_periods@1",),
        )
        self.assertNotIn("capability_plans", analysis_payload)
        self.assertEqual(
            query_payload["analysis_contract_ref"], analysis.analysis_contract_id
        )
        self.assertEqual(query_payload["window_refs"], ("target_day",))
        self.assertEqual(
            query_payload["resolved_windows"][0]["window_id"], "target_day"
        )
        self.assertEqual(plan.minimum_readiness["required_slots"], "ready")
        self.assertEqual(plan.degradation_policy["missing_optional"], "degraded")

    def test_contract_signature_is_order_stable(self):
        left = stable_contract_signature({"b": [2, 1], "a": {"x": 1}})
        right = stable_contract_signature({"a": {"x": 1}, "b": [2, 1]})
        self.assertEqual(left, right)

    def test_query_contract_signature_covers_parameters_and_excludes_identity(self):
        base = {
            "query_contract_id": "query:run-a:1",
            "analysis_contract_ref": "analysis:run-a:1",
            "query_intent": "high_value_scan",
            "dataset_snapshot_refs": ("snapshot:paid:1",),
            "metric_bindings": (),
            "dimension_bindings": (),
            "window_refs": (),
            "resolved_windows": (),
            "filters": (),
            "result_shape": ResultShape((), (), (), ()),
            "completeness_assertions": (),
            "workload_class": "interactive_aggregate",
            "contract_signature": "ignored",
            "query_parameters": {
                "threshold_quantile": 0.95,
                "threshold_reference": "within_window_user_paid_amount",
                "aggregation_grain": ("window_id", "observation_key", "user_id"),
            },
        }
        first = QueryContract(**base)
        second = QueryContract(
            **{
                **base,
                "query_contract_id": "query:run-b:9",
                "analysis_contract_ref": "analysis:run-b:1",
                "contract_signature": "different-ignored-value",
            }
        )

        self.assertEqual(
            query_contract_signature(first), query_contract_signature(second)
        )
        self.assertNotIn("query_contract_id", query_contract_semantic_body(first))
        self.assertNotIn("contract_signature", query_contract_semantic_body(first))
        changed = QueryContract(
            **{
                **base,
                "query_parameters": {
                    **base["query_parameters"],
                    "threshold_quantile": 0.9,
                },
            }
        )
        self.assertNotEqual(
            query_contract_signature(first), query_contract_signature(changed)
        )

    def test_query_contract_codec_requires_current_exact_signed_shape(self):
        window = ResolvedWindow(
            "target_day",
            "target",
            "2026-06-02",
            "2026-06-02",
            "2026-06-03",
            "Africa/Lagos",
            "daily_total",
            1,
            "2026-06-02",
        )
        unsigned = QueryContract(
            query_contract_id="query:codec:1",
            analysis_contract_ref="analysis:codec:1",
            query_intent="daily_metric_baselines",
            dataset_snapshot_refs=("snapshot:paid:1",),
            metric_bindings=(),
            dimension_bindings=(),
            window_refs=("target_day",),
            resolved_windows=(window,),
            filters=(),
            result_shape=ResultShape(
                ("window_id", "paid_amount"),
                ("window_id",),
                ("window_id",),
                ("target_day",),
            ),
            completeness_assertions=("required_windows",),
            workload_class="interactive_aggregate",
            contract_signature="pending",
        )
        signed = replace(
            unsigned,
            contract_signature=query_contract_signature(unsigned),
        )

        self.assertEqual(query_contract_from_dict(signed.to_dict()), signed)
        tampered = signed.to_dict()
        tampered["query_intent"] = "tampered"
        with self.assertRaisesRegex(ValueError, "contract_signature:mismatch"):
            query_contract_from_dict(tampered)
        unknown = {**signed.to_dict(), "legacy_mode": True}
        with self.assertRaisesRegex(ValueError, "keys_invalid"):
            query_contract_from_dict(unknown)


if __name__ == "__main__":
    unittest.main()
