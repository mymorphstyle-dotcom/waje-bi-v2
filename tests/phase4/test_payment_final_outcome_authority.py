from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest

from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    dataset_release_authority_record_from_mapping,
    dataset_snapshot_from_mapping,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from tools.data.build_payment_final_outcome_clickhouse import (
    PaymentFinalOutcomeBuildError,
    PaymentFinalOutcomeManifest,
    PaymentFinalOutcomeProfile,
    build_dataset_snapshot_payload,
    final_outcome_insert_sql,
)
from tests.support.temporal_authority import resolved_test_temporal_authority


ROOT = Path(__file__).resolve().parents[2]


def _profile(**overrides: object) -> PaymentFinalOutcomeProfile:
    values: dict[str, object] = {
        "source_rows": 12,
        "source_unique_orders": 10,
        "successful_orders": 6,
        "not_paid_as_of_snapshot_orders": 4,
        "overlap_orders_resolved_to_success": 2,
        "duplicate_success_rows_removed": 1,
        "successful_start_date_fallback_orders": 1,
        "invalid_source_status_rows": 0,
        "invalid_order_id_rows": 0,
        "invalid_status_date_rows": 0,
        "published_rows": 4,
        "published_terminal_orders": 10,
        "published_successful_orders": 6,
        "published_not_paid_orders": 4,
        "successful_paid_amount_ngn": Decimal("1200.0000"),
        "paid_order_success_rows": 6,
        "paid_order_success_amount_ngn": Decimal("1200.0000"),
        "date_range": ("2026-06-01", "2026-06-02"),
        "status_values": ("order_success", "pay_success"),
    }
    values.update(overrides)
    return PaymentFinalOutcomeProfile(**values)


def _manifest(profile: PaymentFinalOutcomeProfile) -> PaymentFinalOutcomeManifest:
    return PaymentFinalOutcomeManifest.create(
        snapshot_id="payment-final-outcome-test",
        load_revision="payment-final-outcome-load:sha256:" + "a" * 64,
        physical_table="payment_final_outcome_daily__1234567890abcdef",
        schema_fields=(
            "snapshot_id",
            "load_revision",
            "business_date_lagos",
            "final_outcome",
            "payment_method",
            "channel",
            "terminal_orders",
            "successful_paid_amount_ngn",
        ),
        schema_fingerprint="1" * 64,
        rows_content_hash="2" * 64,
        source_checksums={"archive_sha256": "3" * 64},
        profile=profile,
    )


def test_source_contract_exposes_only_reviewed_final_outcomes() -> None:
    contract = load_contract("contracts/sources/payment-final-outcome.source.yaml")

    assert contract["grain"] == [
        "snapshot_id",
        "load_revision",
        "business_date_lagos",
        "final_outcome",
        "payment_method",
        "channel",
    ]
    assert contract["status_authority"]["source_status_mapping"] == {
        "pay_success": "successful",
        "order_success": "not_paid_as_of_snapshot",
    }
    assert contract["status_authority"]["unknown_status_policy"] == "fail_closed"
    assert contract["evidence_boundary"]["unsupported_analyses"] == [
        "failure_reason",
        "failure_stage",
        "retry_chain",
        "payment_processing_latency",
        "provider_or_channel_incident_attribution",
    ]


def test_insert_sql_resolves_success_priority_and_keeps_snapshot_scope() -> None:
    sql = final_outcome_insert_sql(
        physical_table="payment_final_outcome_daily__1234567890abcdef",
        snapshot_id="payment-final-outcome-test",
        load_revision="payment-final-outcome-load:sha256:" + "a" * 64,
    )

    assert "LEFT ANTI JOIN paid_order_success_latest_key_20240101_20260704_v2" in sql
    assert "paid_order_success_clean_20240101_20260704_v2" in sql
    assert "paid_order_detail_raw_20240101_20260704" in sql
    assert "'successful' AS final_outcome" in sql
    assert "'not_paid_as_of_snapshot' AS final_outcome" in sql
    assert "payment_started_ms" in sql
    assert "business_date_lagos" in sql
    assert "join_algorithm = 'grace_hash'" in sql
    assert "failure" not in sql.casefold()


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"status_values": ("pay_success", "unknown")}, "source_status_enum_invalid"),
        ({"invalid_source_status_rows": 1}, "source_status_enum_invalid"),
        ({"invalid_order_id_rows": 1}, "source_order_id_invalid"),
        ({"invalid_status_date_rows": 1}, "source_status_date_invalid"),
        (
            {"published_terminal_orders": 9},
            "terminal_order_reconciliation_failed",
        ),
        (
            {"published_successful_orders": 5},
            "successful_order_reconciliation_failed",
        ),
        (
            {"paid_order_success_amount_ngn": Decimal("1199.0000")},
            "successful_amount_reconciliation_failed",
        ),
    ],
)
def test_profile_fails_closed_on_status_or_reconciliation_drift(
    overrides: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(PaymentFinalOutcomeBuildError, match=error):
        _profile(**overrides)


def test_snapshot_payload_requires_claim_ready_profile() -> None:
    manifest = _manifest(_profile())
    payload = build_dataset_snapshot_payload(manifest)

    assert payload["dataset_id"] == "payment_final_outcome"
    assert payload["evidence_state"] == "claim_ready"
    assert payload["reconciliation_status"] == "matched"
    assert payload["reconciliation_ref"].startswith(
        "payment-final-outcome-reconciliation:sha256:"
    )
    assert payload["row_count"] == 4
    assert payload["date_range"] == ["2026-06-01", "2026-06-02"]
    assert "order_id" not in payload
    assert "user_id" not in payload


def test_runtime_replaces_unbound_attempt_contract_with_final_outcome() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    assert "payment_final_outcome" in registry.dataset_ids
    assert "payment_attempt" not in registry.dataset_ids
    rate = registry.metric("payment_success_rate")
    assert rate["dataset_id"] == "payment_final_outcome"
    assert "sumIf(terminal_orders" in rate["expression"]
    assert rate["numerator_metric"] == "successful_payment_orders"
    assert rate["denominator_metric"] == "terminal_payment_orders"
    capability = registry.capability_inputs("payment_outcome_compare")
    assert capability["allowed_datasets"] == ["payment_final_outcome"]
    assert capability["required_metrics"] == [
        "terminal_payment_orders",
        "successful_payment_orders",
        "not_paid_payment_orders",
        "payment_success_rate",
    ]
    assert capability["allowed_dimensions"] == ["payment_method", "channel"]
    axis = registry.analysis_axis("payment_outcome_health")
    assert axis["capability_refs"] == ["payment_outcome_compare"]
    assert axis["dimension_refs"] == ["payment_method", "channel"]
    assert "payment_outcome_health" in registry.factor_domain(
        "payment_order_metric_chain"
    )["axis_refs"]


def test_real_release_compiles_payment_outcome_queries_for_reviewed_dimensions() -> None:
    payload = json.loads(
        (
            ROOT
            / "artifacts/phase-6/payment-final-outcome-through-2026-07-04/load_manifest.json"
        ).read_text(encoding="utf-8")
    )
    snapshot = dataset_snapshot_from_mapping(payload["dataset_snapshot_payloads"][0])
    release = dataset_release_authority_record_from_mapping(
        payload["dataset_release_authority"]
    )

    class Resolver:
        def resolve_dataset_release(self, release_ref: str):
            if release_ref != release.release_ref:
                raise KeyError(release_ref)
            return release

    resolver = Resolver()
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    outcome = compile_analysis_contract(
        run_id="p6-payment-outcome-real-release",
        proposal={
            "scope": {"type": "full_sample"},
            "grain": "window_id",
            "capability_roles": {
                "payment_outcome_compare": {
                    "analysis_role": "required",
                    "sources": ("payment_outcome_health",),
                }
            },
            "target_metrics": ["terminal_payment_orders"],
            "requested_dimensions": ["payment_method", "channel"],
            "claim_intents": ["comparative_change"],
        },
        accepted_capabilities=("payment_outcome_compare",),
        catalog=DatasetCatalog((snapshot,), release_resolver=resolver),
        registry=registry,
        temporal_authority=resolved_test_temporal_authority(
            time_spec={
                "kind": "date_range",
                "start": "2026-04-01",
                "end": "2026-06-02",
            },
            comparison_spec={
                "kind": "fixed_window",
                "baseline_class": "prior_period",
                "baseline_start": "2026-01-01",
                "baseline_end": "2026-03-04",
                "aggregation": "sum_of_complete_days",
            },
            require_physical_baseline=True,
        ),
        as_of=datetime.fromisoformat("2026-07-06T00:00:00+00:00"),
        release_resolver=resolver,
    )

    assert not outcome.analysis_contract.contract_gaps
    dimension_queries = tuple(
        query for query in outcome.query_contracts if query.dimension_bindings
    )
    assert {
        tuple(binding.dimension_id for binding in query.dimension_bindings)
        for query in dimension_queries
    } == {("payment_method",), ("channel",)}
    assert all(
        query.dataset_snapshot_refs == (snapshot.snapshot_ref,)
        and {binding.metric_id for binding in query.metric_bindings}
        == {
            "terminal_payment_orders",
            "successful_payment_orders",
            "not_paid_payment_orders",
            "payment_success_rate",
        }
        for query in dimension_queries
    )
    assert outcome.capability_plans[0].capability_id == "payment_outcome_compare"
