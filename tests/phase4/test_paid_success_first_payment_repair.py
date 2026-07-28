from __future__ import annotations

import pytest

from tools.data.clean_payment_details_clickhouse import (
    CLEAN_COLUMNS,
    CLEAN_TABLE,
    FIRST_PAYMENT_TABLE,
    RAW_TABLE,
    clean_insert_sql,
    first_payment_insert_sql,
    parse_args,
    should_rebuild_derived,
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


def test_skip_load_reuses_existing_derived_tables_without_truncate() -> None:
    args = parse_args(["--skip-load"])

    assert should_rebuild_derived(args) is False


def test_skip_load_requires_explicit_derived_rebuild_authority() -> None:
    args = parse_args(["--skip-load", "--rebuild-derived"])

    assert should_rebuild_derived(args) is True


def test_fresh_load_still_rebuilds_derived_tables() -> None:
    args = parse_args([])

    assert should_rebuild_derived(args) is True


def test_replace_cannot_drop_derived_tables_on_read_only_reuse_path() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--skip-load", "--replace"])
