from dataclasses import replace
from datetime import date, datetime, timedelta
from copy import deepcopy
from decimal import Decimal
import unittest

from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.analysis_contracts import query_contract_signature
from bi_agent.runtime.answer_package import (
    AuthorityFact,
    _authority_fact_selector,
    _authority_bound_claim_projections,
    build_answer_package,
    reverify_answer_package_for_delivery,
)
from bi_agent.runtime.authoritative_query_chain import AuthoritativeQueryChainError
from bi_agent.runtime.capability_execution import bind_capability_inputs
from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest
from bi_agent.runtime.claim_provenance import build_verified_claim_record
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.evidence_authority import (
    RuntimeEvidenceResolver,
    RuntimeEvidenceAuthority,
    _record_completeness,
    canonical_digest,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.query_completeness import validate_query_result
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.window_metric_evidence import (
    WindowMetricEvidenceError,
    aggregate_window_metric_comparison,
)
from tests.phase4.test_analysis_contract_compiler import _market_dashboard_snapshots


FIXED_WINDOWS = {
    "target_day": ("2026-06-02", "2026-06-02"),
    "previous_day": ("2026-06-01", "2026-06-01"),
    "rolling_7_day_baseline": ("2026-05-26", "2026-06-01"),
    "same_weekday_last_week": ("2026-05-26", "2026-05-26"),
    "pattern_history": ("2026-01-01", "2026-06-02"),
    "anomaly_history": ("2026-05-03", "2026-06-01"),
}


class MarketWindowEvidenceTest(unittest.TestCase):
    def test_authoritative_193_row_query_uses_contract_ordered_primary_baseline(self):
        context = _market_context()

        envelope = execute_capability(_market_request(context))

        self.assertEqual(len(context["result"].rows), 193)
        self.assertEqual(envelope.evidence_type, "statistical_association")
        self.assertEqual(envelope.strength, "directional")
        self.assertEqual(envelope.numeric_facts["target_value"], 120)
        self.assertEqual(envelope.numeric_facts["baseline_value"], 100)
        self.assertEqual(
            envelope.typed_payload["baseline_window_id"],
            "previous_day",
        )

    def test_baseline_order_row_order_and_reference_windows_follow_signed_contract(self):
        context = _market_context()
        contract = context["contract"]
        rows = tuple(reversed(context["result"].rows))

        comparison = aggregate_window_metric_comparison(
            contract,
            rows,
            metric_id="active_users",
        )

        self.assertEqual(comparison.primary_baseline.window_id, "previous_day")
        self.assertEqual(comparison.primary_baseline.value, 100)
        self.assertEqual(
            tuple(item.window_id for item in comparison.comparisons),
            ("rolling_7_day_baseline", "same_weekday_last_week"),
        )
        self.assertEqual(comparison.comparisons[0].value, 100)
        self.assertEqual(len(comparison.comparisons[0].observations), 7)
        self.assertNotIn("pattern_history", repr(comparison.to_payload()))
        self.assertNotIn("anomaly_history", repr(comparison.to_payload()))

        reordered_windows = (
            contract.resolved_windows[0],
            contract.resolved_windows[2],
            contract.resolved_windows[1],
            *contract.resolved_windows[3:],
        )
        reordered_ids = tuple(window.window_id for window in reordered_windows)
        reordered = _resign_contract(
            contract,
            resolved_windows=reordered_windows,
            window_refs=reordered_ids,
            result_shape=replace(
                contract.result_shape,
                required_window_ids=reordered_ids,
            ),
        )
        reordered_comparison = aggregate_window_metric_comparison(
            reordered,
            rows,
            metric_id="active_users",
        )
        self.assertEqual(
            reordered_comparison.primary_baseline.window_id,
            "rolling_7_day_baseline",
        )

    def test_signed_window_order_fields_must_match_exactly(self):
        context = _market_context()
        contract = context["contract"]
        rows = context["result"].rows
        reordered_windows = (
            contract.resolved_windows[0],
            contract.resolved_windows[2],
            contract.resolved_windows[1],
            *contract.resolved_windows[3:],
        )
        reordered_ids = tuple(window.window_id for window in reordered_windows)
        cases = {
            "resolved_windows": _resign_contract(
                contract,
                resolved_windows=reordered_windows,
            ),
            "window_refs": _resign_contract(
                contract,
                window_refs=reordered_ids,
            ),
            "required_window_ids": _resign_contract(
                contract,
                result_shape=replace(
                    contract.result_shape,
                    required_window_ids=reordered_ids,
                ),
            ),
        }
        for label, drifted in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                WindowMetricEvidenceError,
                "window_metric_window_contract_order_invalid",
            ):
                aggregate_window_metric_comparison(
                    drifted,
                    rows,
                    metric_id="active_users",
                )

    def test_raw_authority_fact_keeps_legacy_identity_and_selector_shape(self):
        fact = AuthorityFact.create(
            query_contract_ref="query:legacy:1",
            result_ref="result:legacy:1",
            metric_id="paid_amount",
            value=Decimal("10.00"),
            window_id="target_day",
            window_role="target",
            observation_key="2026-06-02",
            dimensions=(),
            grain=("window_id", "observation_key"),
            value_semantics="raw_scalar",
            display_format="number",
        )
        legacy_payload = {
            "query_contract_ref": "query:legacy:1",
            "result_ref": "result:legacy:1",
            "metric_id": "paid_amount",
            "value": "10.00",
            "window_id": "target_day",
            "window_role": "target",
            "observation_key": "2026-06-02",
            "dimensions": (),
            "grain": ("window_id", "observation_key"),
            "value_semantics": "raw_scalar",
            "display_format": "number",
        }

        self.assertEqual(
            fact.fact_ref,
            f"authority-fact:sha256:{canonical_digest(legacy_payload)}",
        )
        self.assertEqual(fact.observation_keys, ())
        self.assertEqual(fact.source_fact_refs, ())
        self.assertEqual(
            _authority_fact_selector(fact),
            {
                "query_contract_ref": "query:legacy:1",
                "result_ref": "result:legacy:1",
                "metric_id": "paid_amount",
                "window_role": "target",
                "window_id": "target_day",
                "observation_key": "2026-06-02",
                "dimensions": {},
                "grain": ["window_id", "observation_key"],
            },
        )

    def test_aggregate_authority_fact_identity_is_versioned_and_recomputable(self):
        source_ref = "authority-fact:sha256:" + "1" * 64
        observation_keys = ("2026-05-26", "2026-05-27")
        fact = AuthorityFact.create(
            query_contract_ref="query:aggregate:1",
            result_ref="result:aggregate:1",
            metric_id="active_users",
            value=Decimal("100"),
            window_id="rolling_baseline",
            window_role="baseline",
            observation_key="2026-05-26..2026-05-27",
            dimensions=(),
            grain=("window_id", "observation_key"),
            value_semantics="raw_scalar",
            display_format="number",
            aggregation="mean_of_complete_days",
            required_complete_days=2,
            observation_keys=observation_keys,
            source_fact_refs=(source_ref,),
            comparison_ordinal=1,
        )
        identity_payload = {
            "query_contract_ref": "query:aggregate:1",
            "result_ref": "result:aggregate:1",
            "metric_id": "active_users",
            "value": "100",
            "window_id": "rolling_baseline",
            "window_role": "baseline",
            "observation_key": "2026-05-26..2026-05-27",
            "dimensions": (),
            "grain": ("window_id", "observation_key"),
            "value_semantics": "raw_scalar",
            "display_format": "number",
            "fact_schema_version": "window_metric_aggregate_v1",
            "aggregation": "mean_of_complete_days",
            "required_complete_days": 2,
            "observation_keys": observation_keys,
            "source_fact_refs": (source_ref,),
            "comparison_ordinal": 1,
        }

        self.assertEqual(
            fact.fact_ref,
            f"authority-fact:sha256:{canonical_digest(identity_payload)}",
        )
        self.assertEqual(fact.observation_keys, observation_keys)
        self.assertEqual(fact.source_fact_refs, (source_ref,))

    def test_forged_request_rows_cannot_change_authoritative_window_values(self):
        context = _market_context()

        envelope = execute_capability(
            _market_request(
                context,
                params={
                    "rows": (
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "active_users": 999999,
                        },
                    )
                },
            )
        )

        self.assertEqual(envelope.numeric_facts["target_value"], 120)
        self.assertEqual(envelope.numeric_facts["baseline_value"], 100)
        self.assertNotIn("999999", repr(envelope.typed_payload))
        self.assertTrue(envelope.binding_manifest_ref)
        self.assertTrue(envelope.query_execution_record_refs)
        self.assertTrue(envelope.query_execution_record_digests)
        self.assertTrue(envelope.result_refs)
        self.assertTrue(envelope.completeness_record_refs)
        self.assertTrue(envelope.source_snapshot_refs)

    def test_forged_request_rows_cannot_exhaust_authoritative_row_budget(self):
        context = _market_context()
        forged_rows = tuple(
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "active_users": index,
            }
            for index in range(5_001)
        )

        envelope = execute_capability(
            _market_request(context, params={"rows": forged_rows})
        )

        self.assertEqual(envelope.evidence_type, "statistical_association")
        self.assertEqual(envelope.numeric_facts["target_value"], 120)
        self.assertNotIn("row_budget_exceeded", envelope.limitations)

    def test_answer_projection_recomputes_rolling_mean_and_refs_every_observation(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        claim = {
            "text": "Rolling comparison from the supplied answer draft.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {
                "target_value": 120,
                "baseline_value": 100,
                "absolute_change": 20,
                "relative_change": 0.2,
            },
            "fact_selectors": {
                "target_value": {"window_id": "target_day"},
                "baseline_value": {"window_id": "rolling_7_day_baseline"},
                "absolute_change": {
                    "target": {"window_id": "target_day"},
                    "baseline": {"window_id": "rolling_7_day_baseline"},
                },
                "relative_change": {
                    "target": {"window_id": "target_day"},
                    "baseline": {"window_id": "rolling_7_day_baseline"},
                },
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(envelope.to_dict(),),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["numbers"]["baseline_value"], "100")
        self.assertEqual(
            projected[0]["fact_selectors"]["baseline_value"]["aggregation"],
            "mean_of_complete_days",
        )
        rolling_keys = projected[0]["fact_selectors"]["baseline_value"][
            "observation_keys"
        ]
        self.assertEqual(len(rolling_keys), 7)
        self.assertEqual(len(set(projected[0]["fact_refs"])), 8)

    def test_answer_projection_defaults_to_contract_primary_baseline(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        claim = {
            "text": "Primary comparison from the supplied answer draft.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {
                "target_value": 120,
                "baseline_value": 100,
                "absolute_change": 20,
                "relative_change": 0.2,
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(envelope.to_dict(),),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["baseline"]["window_id"], "previous_day")
        self.assertEqual(
            projected[0]["fact_selectors"]["baseline_value"]["window_id"],
            "previous_day",
        )

    def test_compare_periods_answer_projection_uses_evidence_selected_baseline(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        evidence = envelope.to_dict()
        evidence["capability_id"] = "compare_periods"
        evidence["capability"] = "compare_periods"
        evidence["typed_payload"] = {
            **dict(evidence["typed_payload"]),
            "baseline_window_id": "previous_day",
        }
        claim = {
            "text": "Primary comparison from the supplied answer draft.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {
                "target_value": 120,
                "baseline_value": 100,
                "absolute_change": 20,
                "relative_change": 0.2,
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(evidence,),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["baseline"]["window_id"], "previous_day")
        self.assertEqual(
            projected[0]["fact_selectors"]["baseline_value"]["window_id"],
            "previous_day",
        )

    def test_multi_metric_contract_projects_generic_numbers_for_evidence_metric(self):
        context = _market_context(target_metrics=("active_users", "paid_amount"))
        self.assertEqual(
            {binding.metric_id for binding in context["contract"].metric_bindings},
            {"active_users", "paid_amount"},
        )
        envelope = execute_capability(_market_request(context))
        claim = {
            "text": "Primary comparison for the evidence-selected metric.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {
                "target_value": 120,
                "baseline_value": 100,
                "absolute_change": 20,
                "relative_change": 0.2,
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(envelope.to_dict(),),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        selectors = tuple(projected[0]["fact_selectors"].values())
        self.assertEqual(
            {
                selector["metric_id"]
                for selector in selectors
            },
            {"active_users"},
        )
        fact_selector_leaves = tuple(
            leaf
            for selector in selectors
            for leaf in (
                (selector["target"], selector["baseline"])
                if "target" in selector
                else (selector,)
            )
        )
        self.assertEqual(
            {
                selector["query_contract_ref"]
                for selector in fact_selector_leaves
            },
            {context["contract"].query_contract_id},
        )
        self.assertEqual(
            {selector["result_ref"] for selector in fact_selector_leaves},
            {context["result"].result_ref},
        )

    def test_multi_metric_contract_delivery_reverifies_evidence_metric_projection(self):
        context = _market_context(target_metrics=("active_users", "paid_amount"))
        envelope = execute_capability(_market_request(context))

        package = _market_answer_package(context, envelope)
        delivered = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(package["status"], "draft", package)
        self.assertEqual(delivered["status"], "draft", delivered)

    def test_required_complete_days_explicitly_selects_rolling_baseline(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        claim = {
            "text": "Select the complete seven-day baseline.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {"baseline_value": 100},
            "fact_selectors": {
                "baseline_value": {"required_complete_days": 7},
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(envelope.to_dict(),),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertEqual(
            projected[0]["fact_selectors"]["baseline_value"]["window_id"],
            "rolling_7_day_baseline",
        )

    def test_daily_total_selector_matches_raw_compatible_daily_fact(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        claim = {
            "text": "Select the signed previous-day daily total.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {"baseline_value": 100},
            "fact_selectors": {
                "baseline_value": {
                    "window_id": "previous_day",
                    "aggregation": "daily_total",
                },
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(envelope.to_dict(),),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertEqual(
            projected[0]["fact_selectors"]["baseline_value"]["window_id"],
            "previous_day",
        )
        self.assertNotIn(
            "aggregation",
            projected[0]["fact_selectors"]["baseline_value"],
        )

    def test_single_observation_selector_matches_raw_compatible_daily_fact(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        claim = {
            "text": "Select the signed previous-day observation.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {"baseline_value": 100},
            "fact_selectors": {
                "baseline_value": {
                    "window_id": "previous_day",
                    "observation_keys": ["2026-06-01"],
                },
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(envelope.to_dict(),),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertEqual(
            projected[0]["fact_selectors"]["baseline_value"]["window_id"],
            "previous_day",
        )
        self.assertNotIn(
            "observation_keys",
            projected[0]["fact_selectors"]["baseline_value"],
        )

    def test_required_complete_days_fails_closed_when_multiple_baselines_match(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        claim = {
            "text": "A one-day baseline without a window id is ambiguous.",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (envelope.evidence_ref,),
            "numbers": {"baseline_value": 100},
            "fact_selectors": {
                "baseline_value": {"required_complete_days": 1},
            },
        }

        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(envelope.to_dict(),),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(projected, ())
        self.assertTrue(errors)
        self.assertIn(
            "claim_number_fact_not_unique:baseline_value:2",
            str(errors[0].get("reason") or ""),
        )

    def test_delivery_reverify_rejects_numeric_window_aggregation_observation_and_claim_tampering(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        package = _market_answer_package(context, envelope)
        self.assertEqual(package["status"], "draft")
        valid = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )
        self.assertEqual(valid["status"], "draft")

        def mutate_number(claim):
            claim["numbers"]["target_value"] = "121"

        def mutate_window(claim):
            claim["fact_selectors"]["target_value"][
                "window_id"
            ] = "forged_target"

        def mutate_aggregation(claim):
            claim["fact_selectors"]["baseline_value"][
                "aggregation"
            ] = "mean_of_complete_days"

        def mutate_observation(claim):
            claim["fact_selectors"]["baseline_value"]["observation_keys"] = [
                "2026-05-31"
            ]

        def mutate_claim(claim):
            claim["claim_ref"] = "claim:sha256:" + "0" * 64

        for label, mutate in (
            ("numeric", mutate_number),
            ("window", mutate_window),
            ("aggregation", mutate_aggregation),
            ("observation", mutate_observation),
            ("claim", mutate_claim),
        ):
            with self.subTest(label=label):
                tampered = deepcopy(package)
                claim = next(
                    section["payload"]["claims"][0]
                    for section in tampered["sections"]
                    if section["section_id"] == "summary"
                )
                mutate(claim)
                delivered = reverify_answer_package_for_delivery(
                    tampered,
                    evidence_resolver=context["authority"],
                    rows_loader=context["authority"].rows_loader,
                    runtime_registry=context["registry"],
                    release_resolver=context["release_resolver"],
                )
                self.assertEqual(delivered["status"], "failed")
                self.assertEqual(delivered["final_answer"], "")

    def test_pre_aggregate_market_daily_total_package_is_rejected(self):
        context = _market_context()
        envelope = execute_capability(_market_request(context))
        current = _market_answer_package(context, envelope)
        legacy = _pre_aggregate_market_package(context, current)

        delivered = reverify_answer_package_for_delivery(
            legacy,
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )

        claim = legacy["sections"][0]["payload"]["claims"][0]
        self.assertNotIn("aggregation", claim["target"])
        self.assertNotIn("aggregation", claim["baseline"])
        self.assertNotIn(
            "aggregation",
            claim["fact_selectors"]["target_value"],
        )
        self.assertEqual(delivered["status"], "failed", delivered)
        self.assertIn(
            "verified_claim_provenance_invalid",
            {
                item.get("code")
                for item in delivered["admin_audit"]["verifier"]["errors"]
            },
        )

    def test_authority_record_digest_and_signed_contract_tampering_is_rejected(self):
        context = _market_context()
        record_ref = context["bound"].query_execution_record_refs[0]
        original = context["authority"].resolve_query_execution_record(record_ref)
        tampered_contract = replace(
            original.contract,
            resolved_windows=tuple(reversed(original.contract.resolved_windows)),
        )
        variants = {
            "digest": (
                replace(original, record_digest="0" * 64),
                "query_execution_record_digest_mismatch",
            ),
            "query_record": (
                replace(original, query_hash="hash:forged"),
                "query_execution_payload_mismatch",
            ),
            "signature": (
                replace(original, contract=tampered_contract),
                "query_contract_signature_invalid",
            ),
        }
        for label, (tampered_record, integrity_error) in variants.items():
            with self.subTest(label=label):
                resolver = _TamperedQueryResolver(
                    context["authority"],
                    record_ref,
                    tampered_record,
                )
                self.assertIsInstance(resolver, RuntimeEvidenceResolver)
                self.assertIn(
                    integrity_error,
                    runtime_evidence_record_integrity_errors(tampered_record),
                )
                with self.assertRaises(AuthoritativeQueryChainError):
                    execute_capability(
                        _market_request(context, evidence_resolver=resolver)
                    )
                self.assertEqual(resolver.query_record_resolution_count, 1)

    def test_aggregator_fails_closed_for_window_and_observation_drift(self):
        context = _market_context()
        contract = context["contract"]
        rows = tuple(dict(row) for row in context["result"].rows)
        target_index = next(
            index for index, row in enumerate(rows) if row["window_id"] == "target_day"
        )
        rolling_indexes = tuple(
            index
            for index, row in enumerate(rows)
            if row["window_id"] == "rolling_7_day_baseline"
        )
        cases = {
            "window_metric_window_unknown": (
                contract,
                _replace_row(rows, target_index, window_id="unknown_window"),
            ),
            "window_metric_window_role_drift": (
                contract,
                _replace_row(rows, target_index, window_role="baseline"),
            ),
            "window_metric_observation_duplicate": (
                contract,
                (*rows, dict(rows[target_index])),
            ),
            "window_metric_daily_total_incomplete": (
                contract,
                tuple(row for index, row in enumerate(rows) if index != target_index),
            ),
            "window_metric_observation_out_of_range": (
                contract,
                _replace_row(rows, target_index, observation_key="2026-06-03"),
            ),
            "window_metric_complete_days_invalid": (
                contract,
                tuple(row for index, row in enumerate(rows) if index != rolling_indexes[0]),
            ),
            "window_metric_value_invalid": (
                contract,
                _replace_row(rows, target_index, active_users=float("inf")),
            ),
        }
        for expected, (candidate_contract, candidate_rows) in cases.items():
            with self.subTest(expected=expected), self.assertRaisesRegex(
                WindowMetricEvidenceError,
                expected,
            ):
                aggregate_window_metric_comparison(
                    candidate_contract,
                    candidate_rows,
                    metric_id="active_users",
                )

    def test_aggregator_fails_closed_for_target_baseline_and_aggregation_contracts(self):
        context = _market_context()
        contract = context["contract"]
        rows = tuple(dict(row) for row in context["result"].rows)
        windows = tuple(contract.resolved_windows)
        target = next(window for window in windows if window.window_id == "target_day")
        previous = next(window for window in windows if window.window_id == "previous_day")
        same_weekday = next(
            window for window in windows if window.window_id == "same_weekday_last_week"
        )
        no_target = _resign_contract(
            contract,
            resolved_windows=tuple(
                replace(window, role="reference") if window == target else window
                for window in windows
            ),
        )
        no_target_rows = tuple(
            {**row, "window_role": "reference"}
            if row["window_id"] == target.window_id
            else row
            for row in rows
        )
        no_baseline = _resign_contract(
            contract,
            resolved_windows=tuple(
                replace(window, role="reference")
                if window.role == "baseline"
                else window
                for window in windows
            ),
        )
        no_baseline_rows = tuple(
            {**row, "window_role": "reference"}
            if row["window_role"] == "baseline"
            else row
            for row in rows
        )
        multiple_targets = _resign_contract(
            contract,
            resolved_windows=tuple(
                replace(window, role="target") if window == same_weekday else window
                for window in windows
            ),
        )
        multiple_target_rows = tuple(
            {**row, "window_role": "target"}
            if row["window_id"] == same_weekday.window_id
            else row
            for row in rows
        )
        unsupported = _resign_contract(
            contract,
            resolved_windows=tuple(
                replace(previous, aggregation="daily_series")
                if window == previous
                else window
                for window in windows
            ),
        )
        cases = (
            ("window_metric_target_cardinality_invalid", no_target, no_target_rows),
            ("window_metric_baseline_missing", no_baseline, no_baseline_rows),
            (
                "window_metric_target_cardinality_invalid",
                multiple_targets,
                multiple_target_rows,
            ),
            ("window_metric_aggregation_unsupported", unsupported, rows),
        )
        for expected, candidate_contract, candidate_rows in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                WindowMetricEvidenceError,
                expected,
            ):
                aggregate_window_metric_comparison(
                    candidate_contract,
                    candidate_rows,
                    metric_id="active_users",
                )


def _market_context(*, target_metrics=("active_users",)):
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    market, channel = _market_dashboard_snapshots()
    release_ref = dataset_snapshot_release_ref(
        market.logical_snapshot_id,
        market.load_revision,
        (market.snapshot_ref, channel.snapshot_ref),
    )
    released = tuple(
        replace(
            snapshot,
            release_ref=release_ref,
            row_count=193,
            date_range=("2026-01-01", "2026-06-02"),
        )
        for snapshot in (market, channel)
    )
    release_record = build_dataset_release_authority_record(
        tuple({**snapshot.to_dict(), "requires_release": True} for snapshot in released)
    )
    released = tuple(
        replace(snapshot, authority_record_ref=release_record.authority_record_ref)
        for snapshot in released
    )

    class ReleaseResolver:
        def resolve_dataset_release(self, requested_ref):
            if requested_ref != release_record.release_ref:
                raise KeyError(requested_ref)
            return release_record

    release_resolver = ReleaseResolver()
    outcome = compile_analysis_contract(
        run_id="run-market-multi-window",
        proposal={
            "question_families": ("custom_baseline_comparison",),
            "target_metrics": target_metrics,
            "baselines": (
                "previous_day",
                "rolling_7_day_baseline",
                "same_weekday_last_week",
            ),
            "claim_intents": ("comparative_change",),
            "target_semantic": "2026-06-02",
            "fixed_window_bounds": FIXED_WINDOWS,
        },
        accepted_capabilities=("market_health_compare",),
        catalog=DatasetCatalog(released, release_resolver=release_resolver),
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        release_resolver=release_resolver,
    )
    contract = next(
        query
        for query in outcome.query_contracts
        if query.query_intent == "daily_metric_baselines"
    )
    values = {
        "target_day": (120,),
        "previous_day": (100,),
        "rolling_7_day_baseline": (70, 80, 90, 100, 110, 120, 130),
        "same_weekday_last_week": (90,),
    }
    rows = []
    for window in contract.resolved_windows:
        start = date.fromisoformat(window.start_inclusive)
        end = date.fromisoformat(window.end_exclusive)
        day_count = (end - start).days
        window_values = values.get(window.window_id, tuple(50 for _ in range(day_count)))
        for offset, value in enumerate(window_values):
            rows.append(
                {
                    "window_id": window.window_id,
                    "window_role": window.role,
                    "observation_key": (start + timedelta(days=offset)).isoformat(),
                    **{
                        metric_id: (
                            value if metric_id == "active_users" else value * 10
                        )
                        for metric_id in target_metrics
                    },
                }
            )
    authority = RuntimeEvidenceAuthority()
    result = ClickHouseQueryExecutor(
        _FaithfulRowsRuntime(rows),
        evidence_authority=authority,
        release_resolver=release_resolver,
    ).execute(
        contract,
        {released[0].snapshot_ref: released[0]},
        execution_attempt_ref="attempt:market-multi-window",
    )
    report = validate_query_result(
        contract,
        result,
        released[0],
        release_resolver=release_resolver,
    )
    _record_completeness(authority, report)
    bound = bind_capability_inputs(
        outcome.capability_plans[0],
        results={contract.query_contract_id: result},
        reports={contract.query_contract_id: report},
        evidence_authority=authority,
        runtime_registry=registry,
        release_resolver=release_resolver,
    )
    if (bound.status, bound.reasons) != ("ready", ()):
        raise AssertionError((bound.status, bound.reasons, report.failure_reasons))
    return {
        "authority": authority,
        "bound": bound,
        "contract": contract,
        "registry": registry,
        "release_resolver": release_resolver,
        "result": result,
        "report": report,
    }


def _market_request(context, **overrides):
    values = {
        "run_id": "run-market-multi-window",
        "accepted_graph_id": "graph-market-multi-window",
        "graph_version": 1,
        "capability_id": "market_health_compare",
        "question_family": "custom_baseline_comparison",
        "target_claim": "comparative_change",
        "claim_type": "comparative_change",
        "metric": "active_users",
        "scope": "full_sample",
        "time_window": "fixed multi-window",
        "baseline": {"label": "previous day"},
        "target": {"label": "target day"},
        "grain": "window",
        "filters": {},
        "dimensions": (),
        "contract_versions": {},
        "budget_state": BudgetState("ordinary", 0, 50, 100),
        "llm_business_reason": "Compare authoritative reviewed windows.",
        "params": {},
        "bound_input": context["bound"],
        "evidence_resolver": context["authority"],
        "rows_loader": context["authority"].rows_loader,
        "runtime_registry": context["registry"],
        "release_resolver": context["release_resolver"],
    }
    values.update(overrides)
    return CapabilityRequest(**values)


def _market_answer_package(context, envelope):
    claim = {
        "text": "Primary authoritative market comparison.",
        "claim_type": "comparative_change",
        "claim_strength": "observed",
        "evidence_refs": (envelope.evidence_ref,),
        "numbers": {
            "target_value": 120,
            "baseline_value": 100,
            "absolute_change": 20,
            "relative_change": 0.2,
        },
    }
    return build_answer_package(
        run_id="run-market-multi-window",
        draft_claims=(claim,),
        evidence=(envelope.to_dict(),),
        evidence_resolver=context["authority"],
        rows_loader=context["authority"].rows_loader,
        runtime_registry=context["registry"],
        release_resolver=context["release_resolver"],
        checkpoint_events=(),
        proposed_graph=("market_health_compare",),
        accepted_graph=("market_health_compare",),
        rejected_or_degraded_mutations=(),
        validator_results=({"validator": "test", "ok": True},),
        sql_text="",
        sql_hash="hash:market-multi-window",
        artifact_audit={},
        answer_text="Primary authoritative market comparison.",
        final_business_summary="Primary authoritative market comparison.",
    )


def _pre_aggregate_market_package(context, package):
    legacy = deepcopy(package)
    summary = legacy["sections"][0]["payload"]
    evidence = tuple(legacy["sections"][1]["payload"]["evidence"])
    evidence_by_ref = {item["evidence_ref"]: item for item in evidence}
    current_claim = summary["claims"][0]
    contract = context["contract"]
    metric_binding = next(
        binding
        for binding in contract.metric_bindings
        if binding.metric_id == "active_users"
    )

    def raw_fact(window_id):
        row = next(
            item
            for item in context["result"].rows
            if item["window_id"] == window_id
        )
        return AuthorityFact.create(
            query_contract_ref=contract.query_contract_id,
            result_ref=context["result"].result_ref,
            metric_id="active_users",
            value=Decimal(str(row["active_users"])),
            window_id=window_id,
            window_role=str(row["window_role"]),
            observation_key=str(row["observation_key"]),
            dimensions=(),
            grain=tuple(contract.result_shape.grain),
            value_semantics=metric_binding.value_semantics,
            display_format=metric_binding.display_format,
        )

    target = raw_fact("target_day")
    baseline = raw_fact("previous_day")
    provenance_fields = {
        "claim_ref",
        "claim_digest",
        "run_id",
        "context_manifest_ref",
        "result_refs",
        "completeness_record_refs",
        "artifact_refs",
        "memory_refs",
        "reuse_decisions",
        "provenance_record_ref",
    }
    factual = {
        key: deepcopy(value)
        for key, value in current_claim.items()
        if key not in provenance_fields
    }
    factual["fact_refs"] = [
        target.fact_ref,
        baseline.fact_ref,
        target.fact_ref,
        baseline.fact_ref,
        target.fact_ref,
        baseline.fact_ref,
    ]
    factual["fact_selectors"] = _strip_window_aggregate_extensions(
        factual["fact_selectors"]
    )
    factual["target"] = {
        key: value
        for key, value in factual["target"].items()
        if key not in {"aggregation", "required_complete_days"}
    }
    factual["baseline"] = {
        key: value
        for key, value in factual["baseline"].items()
        if key not in {"aggregation", "required_complete_days"}
    }
    provenance = legacy["admin_audit"][
        "trusted_claim_provenance_records"
    ][0]
    summary["claims"][0] = build_verified_claim_record(
        factual,
        run_id=legacy["run_id"],
        context_manifest=legacy["admin_audit"]["context_manifest"],
        evidence_by_ref=evidence_by_ref,
        trusted_provenance=provenance,
    )
    return legacy


def _strip_window_aggregate_extensions(value):
    if isinstance(value, dict):
        return {
            key: _strip_window_aggregate_extensions(item)
            for key, item in value.items()
            if key
            not in {
                "aggregation",
                "required_complete_days",
                "observation_keys",
            }
        }
    if isinstance(value, list):
        return [_strip_window_aggregate_extensions(item) for item in value]
    return value


def _replace_row(rows, index, **updates):
    return tuple(
        {**row, **updates} if row_index == index else row
        for row_index, row in enumerate(rows)
    )


def _resign_contract(contract, **updates):
    candidate = replace(contract, **updates, contract_signature="")
    return replace(candidate, contract_signature=query_contract_signature(candidate))


class _TamperedQueryResolver:
    def __init__(self, authority, record_ref, record):
        self.authority = authority
        self.record_ref = record_ref
        self.record = record
        self.query_record_resolution_count = 0

    def resolve_query_execution(self, result_ref):
        return self.authority.resolve_query_execution(result_ref)

    def resolve_query_execution_record(self, record_ref):
        self.query_record_resolution_count += 1
        if record_ref == self.record_ref:
            return self.record
        return self.authority.resolve_query_execution_record(record_ref)

    def resolve_rows(self, rows_ref):
        return self.authority.resolve_rows(rows_ref)

    def resolve_rows_record(self, record_ref):
        return self.authority.resolve_rows_record(record_ref)

    def resolve_snapshot(self, snapshot_ref):
        return self.authority.resolve_snapshot(snapshot_ref)

    def resolve_completeness(self, record_ref):
        return self.authority.resolve_completeness(record_ref)

    def resolve_capability_binding(self, binding_ref):
        return self.authority.resolve_capability_binding(binding_ref)


class _FaithfulRowsRuntime:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def aggregate(self, sql, query_id, **kwargs):
        return ClickHouseQueryResult(
            ok=True,
            rows=self.rows,
            query_hash=canonical_digest(
                {"sql": sql, "parameters": kwargs.get("parameters", {})}
            ),
            query_id=query_id,
            provider_stats={
                "requested_settings": dict(kwargs.get("settings") or {}),
                "summary": {"read_rows": len(self.rows)},
            },
            execution_attempt_ref=kwargs.get("execution_attempt_ref", ""),
        )


if __name__ == "__main__":
    unittest.main()
