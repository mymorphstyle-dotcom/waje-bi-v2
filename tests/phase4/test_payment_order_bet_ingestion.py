from __future__ import annotations

import csv
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile

import pytest
import yaml

from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.dataset_catalog import dataset_snapshots_from_records
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tools.data.load_payment_order_bet_clickhouse import (
    DATASET_ID,
    PaymentOrderBetLoadError,
    PaymentOrderReconciliation,
    build_dataset_snapshot_payload,
    load_payment_order_bet_rows,
    persist_dataset_snapshot_payload,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_CONTRACT = (
    ROOT / "contracts" / "sources" / "payment-order-bet-link.source.yaml"
)
RUNTIME_BINDINGS = ROOT / "contracts" / "runtime" / "clickhouse-analysis-bindings.yaml"

HEADERS = (
    "订单id",
    "uid",
    "充值时间",
    "充值金额",
    "充值24h截止时间",
    "充值24h内是否游戏",
    "充值24h内下注金额",
    "充值24h内下注金额/充值金额",
    "充值24h内各个玩法的下注金额占比",
    "充值7d内下注金额",
    "充值7d内下注金额/充值金额",
    "充值7d内的各个玩法的下注金额占比",
)


def source_row(**overrides: str) -> list[str]:
    row = {
        "订单id": "order-1",
        "uid": "user-1",
        "充值时间": "2026-06-01 07:00:00",
        "充值金额": "1000.0",
        "充值24h截止时间": "2026-06-02 07:00:00",
        "充值24h内是否游戏": "是",
        "充值24h内下注金额": "1250",
        "充值24h内下注金额/充值金额": "1.25",
        "充值24h内各个玩法的下注金额占比": "Game A:60.00% | Game B:40.00%",
        "充值7d内下注金额": "5000",
        "充值7d内下注金额/充值金额": "5.0",
        "充值7d内的各个玩法的下注金额占比": "Game A:70.00% | Game B:30.00%",
    }
    row.update(overrides)
    return [row[field] for field in HEADERS]


@pytest.fixture
def csv_file():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "支付订单关联下注金额.csv"

        def write(*rows: list[str]) -> Path:
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerows((HEADERS, *rows))
            return path

        yield write


def matched_reconciliation() -> PaymentOrderReconciliation:
    return PaymentOrderReconciliation(
        status="matched",
        source_rows=1,
        paid_order_rows=1,
        matched_rows=1,
        missing_order_rows=0,
        unexpected_order_rows=0,
        user_mismatch_rows=0,
        business_date_mismatch_rows=0,
        paid_amount_mismatch_rows=0,
        source_paid_amount_ngn=Decimal("1000.0000"),
        paid_order_amount_ngn=Decimal("1000.0000"),
        difference_ngn=Decimal("0.0000"),
    )


def test_loader_normalizes_beijing_source_time_to_lagos_business_day(csv_file):
    rows, manifest = load_payment_order_bet_rows(
        csv_file(source_row()), snapshot_id="payment-bet-20260602"
    )

    row = rows.rows[0]
    assert row["business_date_lagos"] == "2026-06-01"
    assert row["payment_completed_at_lagos"] == "2026-06-01 00:00:00.000"
    assert row["paid_amount_ngn"] == Decimal("1000.0000")
    assert row["bet_24h_amount_ngn"] == Decimal("1250.0000")
    assert row["bet_7d_amount_ngn"] == Decimal("5000.0000")
    assert row["played_within_24h"] == 1
    assert manifest.date_range == ("2026-06-01", "2026-06-01")
    assert manifest.source_timezone == "Asia/Shanghai"
    assert manifest.business_timezone == "Africa/Lagos"


def test_loader_treats_reported_ratios_as_two_decimal_audit_fields(csv_file):
    rows, manifest = load_payment_order_bet_rows(
        csv_file(
            source_row(
                **{
                    "充值金额": "3000",
                    "充值24h内下注金额": "50680000",
                    "充值24h内下注金额/充值金额": "16893.33",
                    "充值7d内下注金额": "273596690",
                    "充值7d内下注金额/充值金额": "91198.9",
                }
            )
        ),
        snapshot_id="s1",
    )

    assert rows.rows[0]["reported_bet_to_paid_ratio_24h"] == Decimal("16893.330000")
    assert rows.rows[0]["reported_bet_to_paid_ratio_7d"] == Decimal("91198.900000")
    assert manifest.quality["reported_ratio_rounding_mismatch_rows"] == 0


@pytest.mark.parametrize(
    ("rows", "error"),
    (
        ((source_row(), source_row()), "duplicate_order_id"),
        (
            (source_row(**{"充值24h截止时间": "2026-06-02 07:00:01"}),),
            "payment_24h_deadline_mismatch",
        ),
        (
            (
                source_row(
                    **{
                        "充值24h内下注金额": "5001",
                        "充值24h内下注金额/充值金额": "5.00",
                        "充值7d内下注金额": "5000",
                    }
                ),
            ),
            "bet_window_monotonicity_invalid",
        ),
        (
            (source_row(**{"充值24h内是否游戏": "否"}),),
            "played_flag_amount_mismatch",
        ),
        (
            (source_row(**{"充值24h内下注金额/充值金额": "1.20"}),),
            "reported_ratio_rounding_mismatch",
        ),
    ),
)
def test_loader_fails_closed_on_grain_time_and_window_contracts(csv_file, rows, error):
    with pytest.raises(PaymentOrderBetLoadError, match=error):
        load_payment_order_bet_rows(csv_file(*rows), snapshot_id="s1")


def test_release_requires_exact_paid_order_reconciliation(csv_file):
    _, manifest = load_payment_order_bet_rows(csv_file(source_row()), snapshot_id="s1")

    with pytest.raises(PaymentOrderBetLoadError, match="paid_order_reconciliation_required"):
        build_dataset_snapshot_payload(manifest)

    reconciled = manifest.with_reconciliation(matched_reconciliation())
    payload = build_dataset_snapshot_payload(reconciled)
    assert payload["dataset_id"] == DATASET_ID
    assert payload["status"] == "active"
    assert payload["evidence_state"] == "claim_ready"
    assert payload["reconciliation_status"] == "matched"
    assert payload["row_count"] == 1
    assert payload["physical_table"].startswith("payment_order_bet_link__")
    assert payload["release_ref"].startswith("dataset-release:sha256:")

    mismatch = replace(
        matched_reconciliation(),
        status="mismatch",
        missing_order_rows=1,
        matched_rows=0,
    )
    with pytest.raises(PaymentOrderBetLoadError, match="paid_order_reconciliation_failed"):
        build_dataset_snapshot_payload(manifest.with_reconciliation(mismatch))


def test_snapshot_persists_through_existing_single_release_authority(csv_file):
    _, manifest = load_payment_order_bet_rows(csv_file(source_row()), snapshot_id="s1")
    payload = build_dataset_snapshot_payload(
        manifest.with_reconciliation(matched_reconciliation())
    )
    store = InMemoryConversationStore()

    result = persist_dataset_snapshot_payload(store, payload)

    assert result.active_refs == (payload["snapshot_ref"],)
    typed = dataset_snapshots_from_records(result.verified_payloads)
    assert typed[payload["snapshot_ref"]].dataset_id == DATASET_ID
    assert typed[payload["snapshot_ref"]].authority_record_ref


def test_source_and_runtime_contract_publish_current_data_boundary():
    source = yaml.safe_load(SOURCE_CONTRACT.read_text(encoding="utf-8"))
    runtime = yaml.safe_load(RUNTIME_BINDINGS.read_text(encoding="utf-8"))

    assert source["runtime_binding"]["dataset_id"] == DATASET_ID
    assert source["timestamp_authority"]["source_timezone"] == "Asia/Shanghai"
    assert source["timestamp_authority"]["business_timezone"] == "Africa/Lagos"
    assert source["coverage"]["complete_business_date_end"] == "2026-06-02"
    assert source["evidence_boundary"]["reported_ratio_authority"] == "audit_only"
    assert source["evidence_boundary"]["gameplay_share_authority"] == "context_only"
    assert runtime["datasets"][DATASET_ID]["requires_release"] is True
    assert runtime["datasets"][DATASET_ID]["requires_physical_revision"] is True
    assert (
        runtime["datasets"][DATASET_ID]["current_stage_coverage"]["status"]
        == "accepted_complete_through"
    )
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)
    customer_safe = set(registry.customer_safe_filter_fields(DATASET_ID))
    assert not customer_safe.intersection(
        {"order_id", "user_id", "gameplay_share_24h", "gameplay_share_7d"}
    )
    assert registry.metric(
        "post_payment_bet_24h_amount", dataset_id=DATASET_ID
    )["expression"] == "sum(bet_24h_amount_ngn)"
    assert registry.dimension(
        "amount_bucket", dataset_id=DATASET_ID
    )["source_field"] == "paid_amount_ngn"
    assert registry.capability_inputs("post_payment_behavior_compare")[
        "allowed_datasets"
    ] == [DATASET_ID]
