#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ZIP = Path("/Users/luka/Downloads/dapan_pay_data.zip")
DATABASE = "waje_bi"
RAW_TABLE = "paid_order_detail_raw_20240101_20260704"
CLEAN_TABLE = "paid_order_success_clean_20240101_20260704"
DAILY_TABLE = "paid_order_success_daily_20240101_20260704"
LATEST_TABLE = "paid_order_success_latest_key_20240101_20260704"
METADATA_TABLE = "load_metadata_20240101_20260704"
REPORT_PATH = ROOT / "docs/reviews/full-payment-data-cleaning-20240101-20260704.md"
PROFILE_PATH = ROOT / "artifacts/data-cleaning/payment_details_20240101_20260704_profile.json"

CSV_MEMBERS = (
    "pay_data/2024-01-01_2024-12-31.csv",
    "pay_data/2025-01-01_2025-12-31.csv",
    "pay_data/2026-01-01_2026-07-04.csv",
)


RAW_COLUMNS = """
    `日期` String,
    `支付发起时间` String,
    `支付完成时间` String,
    `订单id` String,
    `用户id` String,
    `是否新用户` String,
    `是否首充` String,
    `支付状态` String,
    `支付发起金额` String,
    `支付成功金额` String,
    `币种` String,
    `分包渠道` String,
    `支付方式` String,
    `国家` String,
    `州/地区` String,
    `城市` String,
    `设备品牌` String,
    `设备型号` String,
    `操作系统` String,
    `网络类型` String,
    `注册时间` String,
    `首充时间` String,
    `支付耗时秒` String
"""


CLEAN_COLUMNS = """
    `order_id` String,
    `user_id` String,
    `business_date_lagos` Date,
    `payment_completed_ms` Int64,
    `payment_started_ms` Nullable(String),
    `paid_amount_ngn` Float64,
    `currency` String,
    `is_new_user` String,
    `is_first_payment` String,
    `channel` Nullable(String),
    `payment_method` String,
    `country` Nullable(String),
    `region` Nullable(String),
    `city` Nullable(String),
    `device_brand` Nullable(String),
    `device_model` Nullable(String),
    `os` Nullable(String),
    `network_type` Nullable(String),
    `registered_at` Nullable(String),
    `first_paid_at` Nullable(String),
    `payment_latency_seconds` Nullable(String),
    `raw_date` String
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", default=str(DEFAULT_ZIP))
    parser.add_argument("--container", default="waje-bi-clickhouse")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--skip-load", action="store_true")
    args = parser.parse_args()

    zip_path = Path(args.zip).expanduser()
    if not zip_path.exists():
        raise SystemExit(f"missing zip: {zip_path}")

    create_tables(args.container, replace=args.replace)
    if not args.skip_load:
        ensure_empty(args.container, RAW_TABLE)
        for member in CSV_MEMBERS:
            load_member(args.container, zip_path, member)

    rebuild_clean_tables(args.container)
    profile = collect_profile(args.container, zip_path)
    write_outputs(profile)
    print(json.dumps(profile["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def ch(container: str, query: str, *, fmt: str | None = None) -> str:
    cmd = [
        "docker",
        "exec",
        container,
        "clickhouse-client",
        "--database",
        DATABASE,
        "--query",
        query,
    ]
    if fmt:
        cmd.extend(["--format", fmt])
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result.stdout.strip()


def create_tables(container: str, *, replace: bool) -> None:
    for table in (RAW_TABLE, CLEAN_TABLE, DAILY_TABLE, LATEST_TABLE, METADATA_TABLE):
        if replace:
            ch(container, f"DROP TABLE IF EXISTS {table}")

    ch(
        container,
        f"""
        CREATE TABLE IF NOT EXISTS {RAW_TABLE}
        ({RAW_COLUMNS})
        ENGINE = MergeTree
        ORDER BY (`支付状态`, `订单id`)
        """,
    )
    ch(
        container,
        f"""
        CREATE TABLE IF NOT EXISTS {CLEAN_TABLE}
        ({CLEAN_COLUMNS})
        ENGINE = MergeTree
        ORDER BY (business_date_lagos, order_id)
        """,
    )
    ch(
        container,
        f"""
        CREATE TABLE IF NOT EXISTS {LATEST_TABLE}
        (
            `order_id` String,
            `payment_completed_ms` Int64
        )
        ENGINE = MergeTree
        ORDER BY order_id
        """,
    )
    ch(
        container,
        f"""
        CREATE TABLE IF NOT EXISTS {DAILY_TABLE}
        (
            `business_date_lagos` Date,
            `paid_orders` UInt64,
            `paid_users` UInt64,
            `paid_amount_ngn` Float64
        )
        ENGINE = MergeTree
        ORDER BY business_date_lagos
        """,
    )
    ch(
        container,
        f"""
        CREATE TABLE IF NOT EXISTS {METADATA_TABLE}
        (
            `key` String,
            `value` String
        )
        ENGINE = TinyLog
        """,
    )


def ensure_empty(container: str, table: str) -> None:
    rows = ch(container, f"SELECT count() FROM {table}")
    if rows != "0":
        raise SystemExit(f"{table} already has {rows} rows; rerun with --replace")


def load_member(container: str, zip_path: Path, member: str) -> None:
    print(f"loading {member}", flush=True)
    unzip = subprocess.Popen(
        ["unzip", "-p", str(zip_path), member],
        stdout=subprocess.PIPE,
    )
    assert unzip.stdout is not None
    insert = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            container,
            "clickhouse-client",
            "--database",
            DATABASE,
            "--query",
            f"INSERT INTO {RAW_TABLE} FORMAT CSVWithNames",
        ],
        stdin=unzip.stdout,
    )
    unzip.stdout.close()
    insert_rc = insert.wait()
    unzip_rc = unzip.wait()
    if insert_rc != 0 or unzip_rc != 0:
        raise SystemExit(
            f"load failed for {member}: unzip={unzip_rc}, clickhouse={insert_rc}"
        )


def rebuild_clean_tables(container: str) -> None:
    ch(container, f"TRUNCATE TABLE {CLEAN_TABLE}")
    ch(container, f"TRUNCATE TABLE {DAILY_TABLE}")
    ch(container, f"TRUNCATE TABLE {LATEST_TABLE}")
    ch(container, f"TRUNCATE TABLE {METADATA_TABLE}")
    ch(container, latest_insert_sql())
    ch(container, clean_insert_sql())
    ch(
        container,
        f"""
        INSERT INTO {DAILY_TABLE}
        SELECT
            business_date_lagos,
            count() AS paid_orders,
            uniqExact(user_id) AS paid_users,
            sum(paid_amount_ngn) AS paid_amount_ngn
        FROM {CLEAN_TABLE}
        GROUP BY business_date_lagos
        """,
    )
    metadata = {
        "raw_rows": f"SELECT toString(count()) FROM {RAW_TABLE}",
        "clean_paid_rows": f"SELECT toString(count()) FROM {CLEAN_TABLE}",
        "clean_paid_amount_ngn": f"SELECT toString(sum(paid_amount_ngn)) FROM {CLEAN_TABLE}",
        "business_date_basis": "SELECT 'payment_completed_ms converted to Africa/Lagos'",
        "dedup_rule": "SELECT 'keep latest pay_success per order_id'",
    }
    values = []
    for key, query in metadata.items():
        value = ch(container, query).replace("'", "\\'")
        values.append(f"('{key}', '{value}')")
    ch(container, f"INSERT INTO {METADATA_TABLE} VALUES {', '.join(values)}")


def latest_insert_sql() -> str:
    return f"""
        INSERT INTO {LATEST_TABLE}
        SELECT
            `订单id` AS order_id,
            max(toInt64OrZero(`支付完成时间`)) AS payment_completed_ms
        FROM {RAW_TABLE}
        WHERE `支付状态` = 'pay_success'
          AND nullIf(nullIf(`订单id`, 'NULL'), '') IS NOT NULL
          AND toInt64OrNull(`支付完成时间`) IS NOT NULL
          AND toFloat64OrNull(`支付成功金额`) IS NOT NULL
        GROUP BY `订单id`
        SETTINGS max_bytes_before_external_group_by = 1000000000
        """


def clean_insert_sql() -> str:
    return f"""
        INSERT INTO {CLEAN_TABLE}
        SELECT
            r.`订单id`,
            r.`用户id`,
            toDate(toTimeZone(fromUnixTimestamp64Milli(toInt64OrZero(r.`支付完成时间`)), 'Africa/Lagos')),
            toInt64OrZero(r.`支付完成时间`),
            nullIf(nullIf(r.`支付发起时间`, 'NULL'), ''),
            toFloat64OrZero(r.`支付成功金额`),
            r.`币种`,
            r.`是否新用户`,
            r.`是否首充`,
            nullIf(nullIf(r.`分包渠道`, 'NULL'), ''),
            r.`支付方式`,
            nullIf(nullIf(r.`国家`, 'NULL'), ''),
            nullIf(nullIf(r.`州/地区`, 'NULL'), ''),
            nullIf(nullIf(r.`城市`, 'NULL'), ''),
            nullIf(nullIf(r.`设备品牌`, 'NULL'), ''),
            nullIf(nullIf(r.`设备型号`, 'NULL'), ''),
            nullIf(nullIf(r.`操作系统`, 'NULL'), ''),
            nullIf(nullIf(r.`网络类型`, 'NULL'), ''),
            nullIf(nullIf(r.`注册时间`, 'NULL'), ''),
            nullIf(nullIf(r.`首充时间`, 'NULL'), ''),
            nullIf(nullIf(r.`支付耗时秒`, 'NULL'), ''),
            r.`日期`
        FROM {RAW_TABLE} AS r
        INNER JOIN {LATEST_TABLE} AS latest
            ON r.`订单id` = latest.order_id
           AND toInt64OrZero(r.`支付完成时间`) = latest.payment_completed_ms
        WHERE r.`支付状态` = 'pay_success'
          AND toInt64OrNull(r.`支付完成时间`) IS NOT NULL
          AND toFloat64OrNull(r.`支付成功金额`) IS NOT NULL
        """


def collect_profile(container: str, zip_path: Path) -> dict:
    def scalar(query: str) -> str:
        return ch(container, query)

    def rows(query: str) -> list[dict]:
        out = ch(container, query, fmt="JSONEachRow")
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    summary = {
        "zip_path": str(zip_path),
        "zip_bytes": zip_path.stat().st_size,
        "raw_table": RAW_TABLE,
        "clean_table": CLEAN_TABLE,
        "daily_table": DAILY_TABLE,
        "latest_table": LATEST_TABLE,
        "raw_rows": int(scalar(f"SELECT count() FROM {RAW_TABLE}")),
        "latest_key_rows": int(scalar(f"SELECT count() FROM {LATEST_TABLE}")),
        "clean_paid_rows": int(scalar(f"SELECT count() FROM {CLEAN_TABLE}")),
        "clean_paid_amount_ngn": float(
            scalar(f"SELECT sum(paid_amount_ngn) FROM {CLEAN_TABLE}")
        ),
        "clean_date_start": scalar(f"SELECT toString(min(business_date_lagos)) FROM {CLEAN_TABLE}"),
        "clean_date_end": scalar(f"SELECT toString(max(business_date_lagos)) FROM {CLEAN_TABLE}"),
        "duplicate_success_rows_removed": int(
            scalar(
                f"""
                SELECT countIf(`支付状态` = 'pay_success'
                  AND toInt64OrNull(`支付完成时间`) IS NOT NULL
                  AND toFloat64OrNull(`支付成功金额`) IS NOT NULL
                  AND nullIf(nullIf(`订单id`, 'NULL'), '') IS NOT NULL)
                  - (SELECT count() FROM {LATEST_TABLE})
                FROM {RAW_TABLE}
                """
            )
        ),
        "clean_rows_over_latest_key": int(
            scalar(
                f"""
                SELECT (SELECT count() FROM {CLEAN_TABLE}) - (SELECT count() FROM {LATEST_TABLE})
                """
            )
        ),
        "invalid_success_rows_excluded": int(
            scalar(
                f"""
                SELECT countIf(`支付状态` = 'pay_success'
                  AND (
                    toInt64OrNull(`支付完成时间`) IS NULL
                    OR toFloat64OrNull(`支付成功金额`) IS NULL
                    OR nullIf(nullIf(`订单id`, 'NULL'), '') IS NULL
                  ))
                FROM {RAW_TABLE}
                """
            )
        ),
        "raw_date_mismatch_success_rows": int(
            scalar(
                f"""
                SELECT countIf(
                    `支付状态` = 'pay_success'
                    AND toInt64OrNull(`支付完成时间`) IS NOT NULL
                    AND toDateOrNull(`日期`) IS NOT NULL
                    AND toDate(toTimeZone(fromUnixTimestamp64Milli(toInt64OrZero(`支付完成时间`)), 'Africa/Lagos')) != toDate(`日期`)
                )
                FROM {RAW_TABLE}
                """
            )
        ),
        "h1_2026_clean_rows_match_existing": scalar(
            f"""
            SELECT if(
                (SELECT count() FROM {CLEAN_TABLE}
                 WHERE business_date_lagos BETWEEN '2026-01-01' AND '2026-06-30')
                =
                (SELECT count() FROM paid_order_success_clean),
                'yes',
                'no'
            )
            """
        ),
        "h1_2026_clean_amount_match_existing": scalar(
            f"""
            SELECT if(
                abs(
                    (SELECT sum(paid_amount_ngn) FROM {CLEAN_TABLE}
                     WHERE business_date_lagos BETWEEN '2026-01-01' AND '2026-06-30')
                    -
                    (SELECT sum(paid_amount_ngn) FROM paid_order_success_clean)
                ) < 0.01,
                'yes',
                'no'
            )
            """
        ),
    }
    return {
        "summary": summary,
        "status_distribution": rows(
            f"""
            SELECT `支付状态` AS status, count() AS rows
            FROM {RAW_TABLE}
            GROUP BY status
            ORDER BY rows DESC
            """
        ),
        "currency_distribution": rows(
            f"""
            SELECT currency, count() AS rows, sum(paid_amount_ngn) AS paid_amount_ngn
            FROM {CLEAN_TABLE}
            GROUP BY currency
            ORDER BY rows DESC
            """
        ),
        "missing_clean_fields": rows(
            f"""
            SELECT field, missing_rows
            FROM
            (
                SELECT 'payment_started_ms' AS field, countIf(isNull(payment_started_ms)) AS missing_rows FROM {CLEAN_TABLE}
                UNION ALL SELECT 'channel', countIf(isNull(channel)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'country', countIf(isNull(country)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'region', countIf(isNull(region)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'city', countIf(isNull(city)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'device_brand', countIf(isNull(device_brand)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'device_model', countIf(isNull(device_model)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'os', countIf(isNull(os)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'network_type', countIf(isNull(network_type)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'registered_at', countIf(isNull(registered_at)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'first_paid_at', countIf(isNull(first_paid_at)) FROM {CLEAN_TABLE}
                UNION ALL SELECT 'payment_latency_seconds', countIf(isNull(payment_latency_seconds)) FROM {CLEAN_TABLE}
            )
            ORDER BY missing_rows DESC
            """
        ),
        "top_payment_methods": rows(
            f"""
            SELECT payment_method, count() AS rows, sum(paid_amount_ngn) AS paid_amount_ngn
            FROM {CLEAN_TABLE}
            GROUP BY payment_method
            ORDER BY rows DESC
            LIMIT 10
            """
        ),
        "top_channels": rows(
            f"""
            SELECT ifNull(channel, '') AS channel, count() AS rows, sum(paid_amount_ngn) AS paid_amount_ngn
            FROM {CLEAN_TABLE}
            GROUP BY channel
            ORDER BY rows DESC
            LIMIT 20
            """
        ),
        "daily_sample": rows(
            f"""
            SELECT business_date_lagos, paid_orders, paid_users, paid_amount_ngn
            FROM {DAILY_TABLE}
            ORDER BY business_date_lagos
            LIMIT 5
            """
        ),
    }


def write_outputs(profile: dict) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(render_report(profile), encoding="utf-8")


def render_report(profile: dict) -> str:
    s = profile["summary"]
    lines = [
        "# Full Payment Data Cleaning Review",
        "",
        "Status: cleaning completed",
        "Scope: 2024-01-01 through 2026-07-04 payment details",
        "",
        "## Outputs",
        "",
        f"- Raw ClickHouse table: `{s['raw_table']}`",
        f"- Clean ClickHouse table: `{s['clean_table']}`",
        f"- Daily summary table: `{s['daily_table']}`",
        f"- Latest-key helper table: `{s['latest_table']}`",
        f"- Profile JSON: `{PROFILE_PATH.relative_to(ROOT)}`",
        "",
        "## Cleaning Rules",
        "",
        "- Include `pay_success` rows for paid amount.",
        "- Exclude `order_success` from paid amount.",
        "- Deduplicate successful payment rows by `订单id`, keeping the latest `支付完成时间`.",
        "- Use `支付完成时间` converted to `Africa/Lagos` as `business_date_lagos`.",
        "- Preserve source dimension values; only empty string and `NULL` are converted to null.",
        "",
        "## Summary",
        "",
        f"- Raw rows loaded: {s['raw_rows']:,}",
        f"- Latest successful order keys: {s['latest_key_rows']:,}",
        f"- Clean paid rows: {s['clean_paid_rows']:,}",
        f"- Clean paid amount: {s['clean_paid_amount_ngn']:,.2f} NGN",
        f"- Clean date range: {s['clean_date_start']} through {s['clean_date_end']}",
        f"- Duplicate success rows removed: {s['duplicate_success_rows_removed']:,}",
        f"- Clean rows above latest-key count: {s['clean_rows_over_latest_key']:,}",
        f"- Invalid success rows excluded: {s['invalid_success_rows_excluded']:,}",
        f"- Raw `日期` mismatch on valid success rows: {s['raw_date_mismatch_success_rows']:,}",
        f"- 2026 H1 row-count match with existing clean table: {s['h1_2026_clean_rows_match_existing']}",
        f"- 2026 H1 amount match with existing clean table: {s['h1_2026_clean_amount_match_existing']}",
        "",
        "## Status Distribution",
        "",
        "| Status | Rows |",
        "|---|---:|",
    ]
    for row in profile["status_distribution"]:
        lines.append(f"| {row['status']} | {fmt_int(row['rows'])} |")
    lines.extend(["", "## Currency Distribution", "", "| Currency | Rows | Paid Amount NGN |", "|---|---:|---:|"])
    for row in profile["currency_distribution"]:
        lines.append(
            f"| {row['currency']} | {fmt_int(row['rows'])} | {fmt_money(row['paid_amount_ngn'])} |"
        )
    lines.extend(["", "## Missing Clean Fields", "", "| Field | Missing Rows |", "|---|---:|"])
    for row in profile["missing_clean_fields"]:
        lines.append(f"| {row['field']} | {fmt_int(row['missing_rows'])} |")
    lines.extend(["", "## Top Payment Methods", "", "| Payment Method | Rows | Paid Amount NGN |", "|---|---:|---:|"])
    for row in profile["top_payment_methods"]:
        lines.append(
            f"| {row['payment_method']} | {fmt_int(row['rows'])} | {fmt_money(row['paid_amount_ngn'])} |"
        )
    lines.extend(["", "## Top Channels", "", "| Channel | Rows | Paid Amount NGN |", "|---|---:|---:|"])
    for row in profile["top_channels"]:
        channel = row["channel"] if row["channel"] else "(blank)"
        lines.append(f"| {channel} | {fmt_int(row['rows'])} | {fmt_money(row['paid_amount_ngn'])} |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This is a new dataset run and does not rewrite the accepted 2026 H1 snapshot.",
            "- `IP` and `设备ID` are still absent from the real payment-detail files.",
            "- This report is data-quality evidence for intake and runtime binding, not a business conclusion.",
            "",
        ]
    )
    return "\n".join(lines)


def fmt_int(value: object) -> str:
    return f"{int(value):,}"


def fmt_money(value: object) -> str:
    return f"{float(value):,.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
