from __future__ import annotations

from tools.data.clean_payment_details_clickhouse import (
    CLEAN_COLUMNS,
    CLEAN_TABLE,
    FIRST_PAYMENT_TABLE,
    RAW_TABLE,
    clean_insert_sql,
    first_payment_insert_sql,
)


def test_first_payment_authority_is_unique_and_deterministic_per_user() -> None:
    sql = first_payment_insert_sql()

    assert f"INSERT INTO {FIRST_PAYMENT_TABLE}" in sql
    assert f"FROM {RAW_TABLE} AS r" in sql
    assert "r.`是否首充` = '1'" in sql
    assert "GROUP BY r.`用户id`" in sql
    assert (
        "argMin(r.`订单id`, tuple(toInt64(r.`支付完成时间`), r.`订单id`))"
        in sql
    )


def test_clean_projection_preserves_source_flag_and_emits_one_canonical_flag() -> None:
    sql = clean_insert_sql()

    assert f"INSERT INTO {CLEAN_TABLE}" in sql
    assert "if(first_payment.order_id != '', '1', '0') AS is_first_payment" in sql
    assert "r.`是否首充` AS is_first_payment_source" in sql
    assert f"LEFT JOIN {FIRST_PAYMENT_TABLE} AS first_payment" in sql
    assert "r.`订单id` = first_payment.order_id" in sql


def test_timestamp_normalization_has_explicit_source_and_business_timezones() -> None:
    sql = clean_insert_sql()

    assert "parseDateTime64BestEffortOrNull" in sql
    assert "'Asia/Shanghai'" in sql
    assert "fromUnixTimestamp64Milli" in first_payment_insert_sql()
    assert "'Africa/Lagos'" in sql
    assert "registered_to_first_paid_lag_seconds" in CLEAN_COLUMNS
    assert "registered_to_first_paid_lag_quality" in CLEAN_COLUMNS


def test_negative_lag_is_auditable_and_does_not_rewrite_source_time() -> None:
    sql = clean_insert_sql()

    assert "'negative_source_anomaly'" in sql
    assert "dateDiff('second', normalized.registered_at, normalized.first_paid_at)" in sql
    assert "addHours" not in sql
    assert "subtractHours" not in sql
