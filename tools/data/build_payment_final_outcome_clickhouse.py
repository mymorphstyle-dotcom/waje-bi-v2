#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.dataset_catalog import dataset_snapshot_release_ref
from tools.data.load_market_dashboard_clickhouse import (
    DockerClickHouseClient,
    SnapshotPersistenceResult,
    persist_dataset_snapshot_payloads,
)
from tools.data.source_loader_common import (
    canonical_json_bytes,
    content_ref,
    file_sha256,
    schema_fingerprint,
)


SOURCE_CONTRACT_PATH = (
    ROOT / "contracts" / "sources" / "payment-final-outcome.source.yaml"
)
SOURCE_ARCHIVE = Path("/Users/luka/Downloads/dapan_pay_data.zip")
DATASET_ID = "payment_final_outcome"
CONTRACT_REF = "contracts/sources/payment-final-outcome.source.yaml@0.1"
RUNTIME_BINDING_REF = "contracts/runtime/clickhouse-analysis-bindings.yaml@23"
RAW_TABLE = "paid_order_detail_raw_20240101_20260704"
SUCCESS_TABLE = "paid_order_success_clean_20240101_20260704_v2"
SUCCESS_KEY_TABLE = "paid_order_success_latest_key_20240101_20260704_v2"
TABLE_PREFIX = "payment_final_outcome_daily"
CANONICALIZATION_VERSION = "payment-final-outcome-v1"
ALLOWED_SOURCE_STATUSES = ("order_success", "pay_success")
ALLOWED_FINAL_OUTCOMES = ("not_paid_as_of_snapshot", "successful")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PaymentFinalOutcomeBuildError(ValueError):
    pass


@dataclass(frozen=True)
class PaymentFinalOutcomeProfile:
    source_rows: int
    source_unique_orders: int
    successful_orders: int
    not_paid_as_of_snapshot_orders: int
    overlap_orders_resolved_to_success: int
    duplicate_success_rows_removed: int
    successful_start_date_fallback_orders: int
    invalid_source_status_rows: int
    invalid_order_id_rows: int
    invalid_status_date_rows: int
    published_rows: int
    published_terminal_orders: int
    published_successful_orders: int
    published_not_paid_orders: int
    successful_paid_amount_ngn: Decimal
    paid_order_success_rows: int
    paid_order_success_amount_ngn: Decimal
    date_range: tuple[str, str]
    status_values: tuple[str, ...]

    def __post_init__(self) -> None:
        count_fields = (
            "source_rows",
            "source_unique_orders",
            "successful_orders",
            "not_paid_as_of_snapshot_orders",
            "overlap_orders_resolved_to_success",
            "duplicate_success_rows_removed",
            "successful_start_date_fallback_orders",
            "invalid_source_status_rows",
            "invalid_order_id_rows",
            "invalid_status_date_rows",
            "published_rows",
            "published_terminal_orders",
            "published_successful_orders",
            "published_not_paid_orders",
            "paid_order_success_rows",
        )
        if any(
            type(getattr(self, field)) is not int or getattr(self, field) < 0
            for field in count_fields
        ):
            raise PaymentFinalOutcomeBuildError("profile_count_invalid")
        if tuple(sorted(self.status_values)) != ALLOWED_SOURCE_STATUSES:
            raise PaymentFinalOutcomeBuildError("source_status_enum_invalid")
        if self.invalid_source_status_rows:
            raise PaymentFinalOutcomeBuildError("source_status_enum_invalid")
        if self.invalid_order_id_rows:
            raise PaymentFinalOutcomeBuildError("source_order_id_invalid")
        if self.invalid_status_date_rows:
            raise PaymentFinalOutcomeBuildError("source_status_date_invalid")
        if (
            self.successful_orders + self.not_paid_as_of_snapshot_orders
            != self.source_unique_orders
            or self.published_terminal_orders != self.source_unique_orders
        ):
            raise PaymentFinalOutcomeBuildError(
                "terminal_order_reconciliation_failed"
            )
        if (
            self.successful_orders != self.paid_order_success_rows
            or self.published_successful_orders != self.successful_orders
        ):
            raise PaymentFinalOutcomeBuildError(
                "successful_order_reconciliation_failed"
            )
        if self.published_not_paid_orders != self.not_paid_as_of_snapshot_orders:
            raise PaymentFinalOutcomeBuildError(
                "not_paid_order_reconciliation_failed"
            )
        if self.successful_paid_amount_ngn != self.paid_order_success_amount_ngn:
            raise PaymentFinalOutcomeBuildError(
                "successful_amount_reconciliation_failed"
            )
        if (
            len(self.date_range) != 2
            or not all(isinstance(value, str) and value for value in self.date_range)
            or self.date_range[0] > self.date_range[1]
        ):
            raise PaymentFinalOutcomeBuildError("profile_date_range_invalid")


@dataclass(frozen=True)
class PaymentFinalOutcomeManifest:
    manifest_ref: str
    snapshot_ref: str
    release_ref: str
    snapshot_id: str
    load_revision: str
    dataset_id: str
    physical_table: str
    watermark: str
    schema_fields: tuple[str, ...]
    schema_fingerprint: str
    rows_content_hash: str
    source_checksums: Mapping[str, str]
    contract_ref: str
    runtime_binding_ref: str
    canonicalization_version: str
    profile: PaymentFinalOutcomeProfile

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        load_revision: str,
        physical_table: str,
        schema_fields: tuple[str, ...],
        schema_fingerprint: str,
        rows_content_hash: str,
        source_checksums: Mapping[str, str],
        profile: PaymentFinalOutcomeProfile,
    ) -> "PaymentFinalOutcomeManifest":
        if not snapshot_id or not load_revision:
            raise PaymentFinalOutcomeBuildError("snapshot_identity_required")
        _require_identifier(physical_table, "physical_table")
        base = {
            "snapshot_id": snapshot_id,
            "load_revision": load_revision,
            "dataset_id": DATASET_ID,
            "physical_table": physical_table,
            "watermark": profile.date_range[1],
            "schema_fields": schema_fields,
            "schema_fingerprint": schema_fingerprint,
            "rows_content_hash": rows_content_hash,
            "source_checksums": dict(source_checksums),
            "contract_ref": CONTRACT_REF,
            "runtime_binding_ref": RUNTIME_BINDING_REF,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "profile": asdict(profile),
        }
        manifest_ref = content_ref("source-load-manifest", base)
        snapshot_ref = content_ref(
            "dataset-snapshot",
            {
                "manifest_ref": manifest_ref,
                "snapshot_id": snapshot_id,
                "load_revision": load_revision,
                "physical_table": physical_table,
                "watermark": profile.date_range[1],
                "schema_fingerprint": schema_fingerprint,
            },
        )
        release_ref = dataset_snapshot_release_ref(
            snapshot_id,
            load_revision,
            (snapshot_ref,),
        )
        return cls(
            manifest_ref=manifest_ref,
            snapshot_ref=snapshot_ref,
            release_ref=release_ref,
            snapshot_id=snapshot_id,
            load_revision=load_revision,
            dataset_id=DATASET_ID,
            physical_table=physical_table,
            watermark=profile.date_range[1],
            schema_fields=schema_fields,
            schema_fingerprint=schema_fingerprint,
            rows_content_hash=rows_content_hash,
            source_checksums=dict(source_checksums),
            contract_ref=CONTRACT_REF,
            runtime_binding_ref=RUNTIME_BINDING_REF,
            canonicalization_version=CANONICALIZATION_VERSION,
            profile=profile,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def final_outcome_insert_sql(
    *,
    physical_table: str,
    snapshot_id: str,
    load_revision: str,
) -> str:
    _require_identifier(physical_table, "physical_table")
    snapshot_literal = _literal(snapshot_id, "snapshot_id")
    revision_literal = _literal(load_revision, "load_revision")
    return f"""
        INSERT INTO {physical_table}
        SELECT
            '{snapshot_literal}' AS snapshot_id,
            '{revision_literal}' AS load_revision,
            business_date_lagos,
            final_outcome,
            payment_method,
            channel,
            count() AS terminal_orders,
            sum(successful_paid_amount_ngn) AS successful_paid_amount_ngn
        FROM
        (
            SELECT
                if(
                    toInt64OrNull(payment_started_ms) IS NOT NULL,
                    toDate(
                        toTimeZone(
                            fromUnixTimestamp64Milli(toInt64(payment_started_ms)),
                            'Africa/Lagos'
                        )
                    ),
                    business_date_lagos
                ) AS business_date_lagos,
                'successful' AS final_outcome,
                payment_method,
                channel,
                toDecimal128(paid_amount_ngn, 4) AS successful_paid_amount_ngn
            FROM {SUCCESS_TABLE}

            UNION ALL

            SELECT
                toDate(
                    toTimeZone(
                        fromUnixTimestamp64Milli(toInt64(r.`支付发起时间`)),
                        'Africa/Lagos'
                    )
                ) AS business_date_lagos,
                'not_paid_as_of_snapshot' AS final_outcome,
                r.`支付方式` AS payment_method,
                nullIf(nullIf(r.`分包渠道`, 'NULL'), '') AS channel,
                toDecimal128(0, 4) AS successful_paid_amount_ngn
            FROM {RAW_TABLE} AS r
            LEFT ANTI JOIN {SUCCESS_KEY_TABLE} AS success_key
                ON r.`订单id` = success_key.order_id
            WHERE r.`支付状态` = 'order_success'
              AND nullIf(nullIf(r.`订单id`, 'NULL'), '') IS NOT NULL
              AND toInt64OrNull(r.`支付发起时间`) IS NOT NULL
        ) AS final_orders
        GROUP BY
            business_date_lagos,
            final_outcome,
            payment_method,
            channel
        SETTINGS join_algorithm = 'grace_hash'
        """


def build_dataset_snapshot_payload(
    manifest: PaymentFinalOutcomeManifest,
) -> dict[str, Any]:
    if type(manifest) is not PaymentFinalOutcomeManifest:
        raise PaymentFinalOutcomeBuildError("payment_final_outcome_manifest_invalid")
    profile = manifest.profile
    reconciliation = {
        "status": "matched",
        "terminal_orders": profile.published_terminal_orders,
        "successful_orders": profile.published_successful_orders,
        "not_paid_as_of_snapshot_orders": profile.published_not_paid_orders,
        "successful_paid_amount_ngn": str(profile.successful_paid_amount_ngn),
        "paid_order_success_rows": profile.paid_order_success_rows,
        "paid_order_success_amount_ngn": str(
            profile.paid_order_success_amount_ngn
        ),
    }
    reconciliation_ref = content_ref(
        "payment-final-outcome-reconciliation",
        reconciliation,
    )
    return {
        "snapshot_ref": manifest.snapshot_ref,
        "dataset_id": manifest.dataset_id,
        "physical_table": manifest.physical_table,
        "watermark": manifest.watermark,
        "schema_fingerprint": manifest.schema_fingerprint,
        "schema_fields": list(manifest.schema_fields),
        "contract_ref": manifest.contract_ref,
        "loaded_at": datetime.combine(
            date.fromisoformat(manifest.watermark) + timedelta(days=1),
            time.min,
            tzinfo=timezone.utc,
        ).isoformat(),
        "status": "active",
        "evidence_state": "claim_ready",
        "reconciliation_status": "matched",
        "reconciliation_ref": reconciliation_ref,
        "logical_snapshot_id": manifest.snapshot_id,
        "load_revision": manifest.load_revision,
        "release_ref": manifest.release_ref,
        "rows_content_hash": manifest.rows_content_hash,
        "snapshot_id": manifest.snapshot_id,
        "source_load_manifest_ref": manifest.manifest_ref,
        "runtime_binding_ref": manifest.runtime_binding_ref,
        "source_checksums": dict(manifest.source_checksums),
        "row_count": profile.published_rows,
        "date_range": list(profile.date_range),
        "no_data_partitions": [],
        "no_data_partition_windows": [],
        "requires_release": True,
        "reconciliation": reconciliation,
    }


def build_payment_final_outcome(
    client: Any,
    *,
    snapshot_id: str,
    archive_path: Path = SOURCE_ARCHIVE,
) -> PaymentFinalOutcomeManifest:
    contract = load_contract(SOURCE_CONTRACT_PATH)
    schema = _clean_schema(contract)
    fingerprint = schema_fingerprint(tuple(f"{name}:{kind}" for name, kind in schema))
    physical_table = f"{TABLE_PREFIX}__{fingerprint[:16]}"
    source_checksum = file_sha256(archive_path)
    expected_checksum = str(
        _mapping(contract.get("source_lineage"), "source_lineage_invalid").get(
            "source_archive_sha256"
        )
        or ""
    )
    if source_checksum != expected_checksum:
        raise PaymentFinalOutcomeBuildError("source_archive_checksum_mismatch")
    load_revision = content_ref(
        "payment-final-outcome-load",
        {
            "snapshot_id": snapshot_id,
            "source_archive_sha256": source_checksum,
            "schema_fingerprint": fingerprint,
            "contract_ref": CONTRACT_REF,
            "canonicalization_version": CANONICALIZATION_VERSION,
        },
    )
    _validate_source(client)
    _apply_ddl(client, physical_table=physical_table, schema=schema)
    existing = _scalar(
        client,
        f"""
        SELECT count() AS value
        FROM {physical_table}
        WHERE snapshot_id = {{snapshot_id:String}}
          AND load_revision = {{load_revision:String}}
        """,
        parameters={"snapshot_id": snapshot_id, "load_revision": load_revision},
    )
    if int(existing) == 0:
        client.command(
            final_outcome_insert_sql(
                physical_table=physical_table,
                snapshot_id=snapshot_id,
                load_revision=load_revision,
            )
        )
    profile, rows_hash = _collect_profile(
        client,
        physical_table=physical_table,
        snapshot_id=snapshot_id,
        load_revision=load_revision,
    )
    return PaymentFinalOutcomeManifest.create(
        snapshot_id=snapshot_id,
        load_revision=load_revision,
        physical_table=physical_table,
        schema_fields=tuple(name for name, _ in schema),
        schema_fingerprint=fingerprint,
        rows_content_hash=rows_hash,
        source_checksums={"archive_sha256": source_checksum},
        profile=profile,
    )


def _validate_source(client: Any) -> None:
    rows = _query_rows(
        client,
        f"""
        SELECT
            arraySort(groupUniqArray(`支付状态`)) AS status_values,
            countIf(`支付状态` NOT IN ('pay_success', 'order_success')) AS invalid_source_status_rows,
            countIf(nullIf(nullIf(`订单id`, 'NULL'), '') IS NULL) AS invalid_order_id_rows,
            countIf(
                (`支付状态` = 'pay_success' AND toInt64OrNull(`支付完成时间`) IS NULL)
                OR (`支付状态` = 'order_success' AND toInt64OrNull(`支付发起时间`) IS NULL)
            ) AS invalid_status_date_rows
        FROM {RAW_TABLE}
        """,
    )
    if len(rows) != 1:
        raise PaymentFinalOutcomeBuildError("source_profile_shape_invalid")
    row = rows[0]
    if tuple(row.get("status_values") or ()) != ALLOWED_SOURCE_STATUSES:
        raise PaymentFinalOutcomeBuildError("source_status_enum_invalid")
    if int(row.get("invalid_source_status_rows") or 0):
        raise PaymentFinalOutcomeBuildError("source_status_enum_invalid")
    if int(row.get("invalid_order_id_rows") or 0):
        raise PaymentFinalOutcomeBuildError("source_order_id_invalid")
    if int(row.get("invalid_status_date_rows") or 0):
        raise PaymentFinalOutcomeBuildError("source_status_date_invalid")


def _apply_ddl(
    client: Any,
    *,
    physical_table: str,
    schema: tuple[tuple[str, str], ...],
) -> None:
    _require_identifier(physical_table, "physical_table")
    columns = ",\n".join(f"`{name}` {kind}" for name, kind in schema)
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {physical_table}
        ({columns})
        ENGINE = MergeTree
        ORDER BY (
            snapshot_id,
            load_revision,
            business_date_lagos,
            final_outcome,
            payment_method,
            ifNull(channel, '')
        )
        """
    )
    actual = tuple(
        (str(row["name"]), str(row["type"]))
        for row in _query_rows(
            client,
            """
            SELECT name, type
            FROM system.columns
            WHERE database = currentDatabase()
              AND table = {table:String}
            ORDER BY position
            """,
            parameters={"table": physical_table},
        )
    )
    if actual != schema:
        raise PaymentFinalOutcomeBuildError("clickhouse_schema_drift")


def _collect_profile(
    client: Any,
    *,
    physical_table: str,
    snapshot_id: str,
    load_revision: str,
) -> tuple[PaymentFinalOutcomeProfile, str]:
    source = _one(
        client,
        f"""
        SELECT
          count() AS source_rows,
          countIf(`支付状态` = 'pay_success') AS raw_success_rows,
          countIf(`支付状态` = 'order_success') AS raw_order_success_rows,
          countIf(`支付状态` NOT IN ('pay_success', 'order_success')) AS invalid_source_status_rows,
          countIf(nullIf(nullIf(`订单id`, 'NULL'), '') IS NULL) AS invalid_order_id_rows,
          countIf(
              (`支付状态` = 'pay_success' AND toInt64OrNull(`支付完成时间`) IS NULL)
              OR (`支付状态` = 'order_success' AND toInt64OrNull(`支付发起时间`) IS NULL)
          ) AS invalid_status_date_rows,
          arraySort(groupUniqArray(`支付状态`)) AS status_values
        FROM {RAW_TABLE}
        """,
        "source_profile_shape_invalid",
    )
    success = _one(
        client,
        f"""
        SELECT
          count() AS successful_orders,
          countIf(toInt64OrNull(payment_started_ms) IS NULL) AS successful_start_date_fallback_orders,
          sum(toDecimal128(paid_amount_ngn, 4)) AS successful_paid_amount_ngn
        FROM {SUCCESS_TABLE}
        """,
        "success_profile_shape_invalid",
    )
    published = _one(
        client,
        f"""
        SELECT
          count() AS published_rows,
          sum(terminal_orders) AS published_terminal_orders,
          sumIf(terminal_orders, final_outcome = 'successful') AS published_successful_orders,
          sumIf(terminal_orders, final_outcome = 'not_paid_as_of_snapshot') AS published_not_paid_orders,
          sum(successful_paid_amount_ngn) AS published_successful_paid_amount_ngn,
          toString(min(business_date_lagos)) AS min_date,
          toString(max(business_date_lagos)) AS max_date,
          groupBitXor(cityHash64(tuple(business_date_lagos, final_outcome, payment_method, ifNull(channel, ''), terminal_orders, successful_paid_amount_ngn))) AS hash_a,
          groupBitXor(cityHash64(tuple(business_date_lagos, final_outcome, payment_method, ifNull(channel, ''), terminal_orders, successful_paid_amount_ngn), 1)) AS hash_b
        FROM {physical_table}
        WHERE snapshot_id = {{snapshot_id:String}}
          AND load_revision = {{load_revision:String}}
        """,
        "published_profile_shape_invalid",
        parameters={"snapshot_id": snapshot_id, "load_revision": load_revision},
    )
    successful_orders = int(success["successful_orders"])
    not_paid_orders = int(published["published_not_paid_orders"])
    raw_success_rows = int(source["raw_success_rows"])
    raw_order_success_rows = int(source["raw_order_success_rows"])
    accepted_profile = _mapping(
        load_contract(SOURCE_CONTRACT_PATH).get("accepted_snapshot_profile"),
        "accepted_snapshot_profile_invalid",
    )
    expected = {
        "source_rows": int(accepted_profile.get("source_rows") or -1),
        "source_unique_orders": int(
            accepted_profile.get("source_unique_orders") or -1
        ),
        "successful_orders": int(accepted_profile.get("successful_orders") or -1),
        "not_paid_as_of_snapshot_orders": int(
            accepted_profile.get("not_paid_as_of_snapshot_orders") or -1
        ),
        "overlap_orders_resolved_to_success": int(
            accepted_profile.get("overlap_orders_resolved_to_success") or -1
        ),
        "duplicate_success_rows_removed": int(
            accepted_profile.get("duplicate_success_rows_removed") or -1
        ),
        "successful_start_date_fallback_orders": int(
            accepted_profile.get("successful_start_date_fallback_orders") or -1
        ),
        "published_aggregate_rows": int(
            accepted_profile.get("published_aggregate_rows") or -1
        ),
        "successful_paid_amount_ngn": _decimal(
            accepted_profile.get("successful_paid_amount_ngn"),
            "accepted_successful_paid_amount",
        ),
        "date_range": tuple(str(value) for value in accepted_profile.get("date_range") or ()),
    }
    observed = {
        "source_rows": int(source["source_rows"]),
        "source_unique_orders": successful_orders + not_paid_orders,
        "successful_orders": successful_orders,
        "not_paid_as_of_snapshot_orders": not_paid_orders,
        "overlap_orders_resolved_to_success": raw_order_success_rows
        - not_paid_orders,
        "duplicate_success_rows_removed": raw_success_rows - successful_orders,
        "successful_start_date_fallback_orders": int(
            success["successful_start_date_fallback_orders"]
        ),
        "published_aggregate_rows": int(published["published_rows"]),
        "successful_paid_amount_ngn": _decimal(
            published["published_successful_paid_amount_ngn"],
            "published_successful_amount",
        ),
        "date_range": (str(published["min_date"]), str(published["max_date"])),
    }
    if observed != expected:
        raise PaymentFinalOutcomeBuildError("accepted_snapshot_profile_mismatch")
    profile = PaymentFinalOutcomeProfile(
        source_rows=int(source["source_rows"]),
        source_unique_orders=successful_orders + not_paid_orders,
        successful_orders=successful_orders,
        not_paid_as_of_snapshot_orders=not_paid_orders,
        overlap_orders_resolved_to_success=raw_order_success_rows - not_paid_orders,
        duplicate_success_rows_removed=raw_success_rows - successful_orders,
        successful_start_date_fallback_orders=int(
            success["successful_start_date_fallback_orders"]
        ),
        invalid_source_status_rows=int(source["invalid_source_status_rows"]),
        invalid_order_id_rows=int(source["invalid_order_id_rows"]),
        invalid_status_date_rows=int(source["invalid_status_date_rows"]),
        published_rows=int(published["published_rows"]),
        published_terminal_orders=int(published["published_terminal_orders"]),
        published_successful_orders=int(published["published_successful_orders"]),
        published_not_paid_orders=int(published["published_not_paid_orders"]),
        successful_paid_amount_ngn=_decimal(
            published["published_successful_paid_amount_ngn"],
            "published_successful_amount",
        ),
        paid_order_success_rows=successful_orders,
        paid_order_success_amount_ngn=_decimal(
            success["successful_paid_amount_ngn"], "paid_order_success_amount"
        ),
        date_range=(str(published["min_date"]), str(published["max_date"])),
        status_values=tuple(str(value) for value in source["status_values"]),
    )
    rows_hash = hashlib.sha256(
        canonical_json_bytes(
            {
                "algorithm": "clickhouse-aggregate-cityhash64-xor-v1",
                "published_rows": profile.published_rows,
                "published_terminal_orders": profile.published_terminal_orders,
                "hash_a": str(published["hash_a"]),
                "hash_b": str(published["hash_b"]),
            }
        )
    ).hexdigest()
    return profile, rows_hash


def _clean_schema(contract: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    storage = _mapping(contract.get("storage_boundary"), "storage_boundary_invalid")
    raw = storage.get("clean_schema")
    if not isinstance(raw, list) or not raw:
        raise PaymentFinalOutcomeBuildError("clean_schema_invalid")
    values: list[tuple[str, str]] = []
    for item in raw:
        mapping = _mapping(item, "clean_schema_invalid")
        name = str(mapping.get("name") or "")
        kind = str(mapping.get("type") or "")
        _require_identifier(name, "schema_field")
        if not kind:
            raise PaymentFinalOutcomeBuildError("schema_type_invalid")
        values.append((name, kind))
    return tuple(values)


def _query_rows(
    client: Any,
    query: str,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(row)
        for row in client.query(query, parameters=parameters or {}).named_results()
    )


def _one(
    client: Any,
    query: str,
    error: str,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    rows = _query_rows(client, query, parameters=parameters)
    if len(rows) != 1:
        raise PaymentFinalOutcomeBuildError(error)
    return rows[0]


def _scalar(
    client: Any,
    query: str,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> Any:
    row = _one(client, query, "scalar_query_shape_invalid", parameters=parameters)
    if tuple(row) != ("value",):
        raise PaymentFinalOutcomeBuildError("scalar_query_field_invalid")
    return row["value"]


def _mapping(value: Any, error: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PaymentFinalOutcomeBuildError(error)
    return value


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise PaymentFinalOutcomeBuildError(f"{field}_invalid") from exc


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PaymentFinalOutcomeBuildError(f"{field}_invalid")


def _literal(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in ("'", "\\", "\n", "\r"))
    ):
        raise PaymentFinalOutcomeBuildError(f"{field}_invalid")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--manifest-out", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, default=SOURCE_ARCHIVE)
    parser.add_argument("--clickhouse-container", default="")
    parser.add_argument("--skip-postgres", action="store_true")
    args = parser.parse_args(argv)

    if args.clickhouse_container:
        database = os.environ.get("WAJE_CLICKHOUSE_DATABASE", "")
        if not database:
            raise PaymentFinalOutcomeBuildError("missing_clickhouse_database")
        client = DockerClickHouseClient(args.clickhouse_container, database)
    else:
        runtime = ClickHouseRuntime.from_env()
        if not runtime.configured():
            raise PaymentFinalOutcomeBuildError(
                "missing_clickhouse_binding:" + ",".join(runtime.binding.missing)
            )
        client = runtime._get_client()

    manifest = build_payment_final_outcome(
        client,
        snapshot_id=args.snapshot_id,
        archive_path=args.source_archive,
    )
    payload = build_dataset_snapshot_payload(manifest)
    persistence = SnapshotPersistenceResult(active_refs=(), superseded_refs=())
    store = None if args.skip_postgres else PostgresConversationStore.from_env()
    try:
        if store is not None:
            with store.dataset_snapshot_release_lock(manifest.snapshot_id):
                persistence = persist_dataset_snapshot_payloads(store, (payload,))
    finally:
        if store is not None:
            close = getattr(store.connection, "close", None)
            if callable(close):
                close()
    artifact = {
        "source_load_manifest": manifest.to_dict(),
        "dataset_snapshot_payloads": list(
            persistence.verified_payloads or (payload,)
        ),
        "dataset_release_authority": persistence.authority_record,
    }
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_text(
        json.dumps(
            json.loads(canonical_json_bytes(artifact)),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "manifest_path": str(args.manifest_out),
                "manifest_ref": manifest.manifest_ref,
                "snapshot_ref": manifest.snapshot_ref,
                "release_ref": manifest.release_ref,
                "row_count": manifest.profile.published_rows,
                "terminal_orders": manifest.profile.published_terminal_orders,
                "successful_orders": manifest.profile.published_successful_orders,
                "not_paid_as_of_snapshot_orders": manifest.profile.published_not_paid_orders,
                "date_range": manifest.profile.date_range,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
