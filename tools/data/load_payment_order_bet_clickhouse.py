#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


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
    insert_json_each_row,
    schema_fingerprint,
)


SOURCE_CONTRACT_PATH = (
    ROOT / "contracts" / "sources" / "payment-order-bet-link.source.yaml"
)
DEFAULT_SOURCE = Path("/Users/luka/Downloads/支付订单关联下注金额.csv")
DATASET_ID = "payment_order_bet_link"
TABLE_PREFIX = "payment_order_bet_link"
CONTRACT_REF = "contracts/sources/payment-order-bet-link.source.yaml@0.1"
RUNTIME_BINDING_REF = "contracts/runtime/clickhouse-analysis-bindings.yaml@23"
TABLE_ENGINE = "MergeTree"
ORDER_BY = (
    "snapshot_id",
    "load_revision",
    "business_date_lagos",
    "order_id",
)
CANONICALIZATION_VERSION = "payment-order-bet-link-v1"
SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")
BUSINESS_TIMEZONE = ZoneInfo("Africa/Lagos")
RATIO_TOLERANCE = Decimal("0.005")
AMOUNT_QUANTUM = Decimal("0.0001")
RATIO_QUANTUM = Decimal("0.000001")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PaymentOrderBetLoadError(ValueError):
    pass


@dataclass(frozen=True)
class PaymentOrderReconciliation:
    status: str
    source_rows: int
    paid_order_rows: int
    matched_rows: int
    missing_order_rows: int
    unexpected_order_rows: int
    user_mismatch_rows: int
    business_date_mismatch_rows: int
    paid_amount_mismatch_rows: int
    source_paid_amount_ngn: Decimal
    paid_order_amount_ngn: Decimal
    difference_ngn: Decimal

    def __post_init__(self) -> None:
        if self.status not in {"matched", "mismatch", "not_checked"}:
            raise PaymentOrderBetLoadError("paid_order_reconciliation_status_invalid")
        counts = (
            self.source_rows,
            self.paid_order_rows,
            self.matched_rows,
            self.missing_order_rows,
            self.unexpected_order_rows,
            self.user_mismatch_rows,
            self.business_date_mismatch_rows,
            self.paid_amount_mismatch_rows,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise PaymentOrderBetLoadError("paid_order_reconciliation_count_invalid")


@dataclass(frozen=True)
class PaymentOrderBetRows:
    snapshot_id: str
    load_revision: str
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PaymentOrderBetLoadManifest:
    manifest_ref: str
    snapshot_ref: str
    release_ref: str
    snapshot_id: str
    load_revision: str
    dataset_id: str
    physical_table: str
    watermark: str
    row_count: int
    unique_order_count: int
    unique_user_count: int
    date_range: tuple[str, str]
    source_time_range: tuple[str, str]
    source_timezone: str
    business_timezone: str
    schema_fields: tuple[str, ...]
    schema_fingerprint: str
    rows_content_hash: str
    source_checksums: Mapping[str, str]
    contract_ref: str
    runtime_binding_ref: str
    canonicalization_version: str
    quality: Mapping[str, Any]
    reconciliation_ref: str
    reconciliation: PaymentOrderReconciliation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_reconciliation(
        self,
        reconciliation: PaymentOrderReconciliation,
    ) -> "PaymentOrderBetLoadManifest":
        if type(reconciliation) is not PaymentOrderReconciliation:
            raise PaymentOrderBetLoadError("paid_order_reconciliation_invalid")
        reconciliation_ref = content_ref(
            "payment-order-reconciliation",
            asdict(reconciliation),
        )
        base = {
            key: value
            for key, value in self.to_dict().items()
            if key
            not in {
                "manifest_ref",
                "snapshot_ref",
                "release_ref",
                "reconciliation_ref",
                "reconciliation",
            }
        }
        manifest_ref = content_ref(
            "source-load-manifest",
            {
                **base,
                "reconciliation_ref": reconciliation_ref,
                "reconciliation": asdict(reconciliation),
            },
        )
        snapshot_ref = _snapshot_ref(
            manifest_ref=manifest_ref,
            snapshot_id=self.snapshot_id,
            load_revision=self.load_revision,
            physical_table=self.physical_table,
            watermark=self.watermark,
            fingerprint=self.schema_fingerprint,
        )
        release_ref = dataset_snapshot_release_ref(
            self.snapshot_id,
            self.load_revision,
            (snapshot_ref,),
        )
        return replace(
            self,
            manifest_ref=manifest_ref,
            snapshot_ref=snapshot_ref,
            release_ref=release_ref,
            reconciliation_ref=reconciliation_ref,
            reconciliation=reconciliation,
        )


def load_payment_order_bet_rows(
    source_path: str | Path,
    *,
    snapshot_id: str,
    source_contract_path: str | Path = SOURCE_CONTRACT_PATH,
) -> tuple[PaymentOrderBetRows, PaymentOrderBetLoadManifest]:
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise PaymentOrderBetLoadError("snapshot_id_required")
    path = Path(source_path)
    if not path.is_file():
        raise PaymentOrderBetLoadError(f"source_file_missing:{path}")
    contract = load_contract(source_contract_path)
    mapping = _field_mapping(contract)
    expected_headers = tuple(mapping.values())
    clean_schema = _clean_schema(contract)
    schema_descriptor = _schema_descriptor(clean_schema)
    fingerprint = schema_fingerprint(schema_descriptor)
    physical_table = _versioned_table_name(TABLE_PREFIX, fingerprint)
    checksum = file_sha256(path)
    contract_source = _mapping(contract.get("source_file"), "source_file_invalid")
    expected_checksum = str(contract_source.get("sha256") or "")
    contract_source_path = Path(str(contract_source.get("path") or ""))
    if path.resolve() == contract_source_path.resolve() and checksum != expected_checksum:
        raise PaymentOrderBetLoadError("source_checksum_mismatch")

    rows: list[dict[str, Any]] = []
    order_ids: set[str] = set()
    user_ids: set[str] = set()
    business_dates: list[str] = []
    source_times: list[str] = []
    paid_total = Decimal(0)
    bet_24h_total = Decimal(0)
    bet_7d_total = Decimal(0)
    gameplay_share_rounding_outliers = 0
    rows_hasher = hashlib.sha256()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        _validate_headers(reader.fieldnames, expected_headers)
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise PaymentOrderBetLoadError(
                    f"source_row_extra_cells:row={row_number}"
                )
            if any(raw.get(header) is None for header in expected_headers):
                raise PaymentOrderBetLoadError(
                    f"source_row_missing_cells:row={row_number}"
                )
            row = _normalize_row(raw, mapping=mapping, row_number=row_number)
            order_id = row["order_id"]
            if order_id in order_ids:
                raise PaymentOrderBetLoadError(f"duplicate_order_id:{order_id}")
            order_ids.add(order_id)
            user_ids.add(row["user_id"])
            business_dates.append(row["business_date_lagos"])
            source_times.append(str(raw[mapping["payment_completed_at_source"]]).strip())
            paid_total += row["paid_amount_ngn"]
            bet_24h_total += row["bet_24h_amount_ngn"]
            bet_7d_total += row["bet_7d_amount_ngn"]
            gameplay_share_rounding_outliers += int(
                _gameplay_share_rounding_outlier(row["gameplay_share_24h"])
                or _gameplay_share_rounding_outlier(row["gameplay_share_7d"])
            )
            rows.append(row)
            rows_hasher.update(canonical_json_bytes(row))
            rows_hasher.update(b"\n")
    if not rows:
        raise PaymentOrderBetLoadError("source_has_no_data")

    quality = {
        "duplicate_order_rows": 0,
        "negative_amount_rows": 0,
        "bet_24h_greater_than_7d_rows": 0,
        "played_flag_amount_mismatch_rows": 0,
        "reported_ratio_rounding_mismatch_rows": 0,
        "gameplay_share_rounding_outlier_rows": gameplay_share_rounding_outliers,
        "paid_amount_ngn": paid_total,
        "bet_24h_amount_ngn": bet_24h_total,
        "bet_7d_amount_ngn": bet_7d_total,
    }
    rows_hash = rows_hasher.hexdigest()
    load_revision = content_ref(
        "payment-order-bet-load",
        {
            "snapshot_id": snapshot_id,
            "source_checksum": checksum,
            "rows_content_hash": rows_hash,
            "schema_fingerprint": fingerprint,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "contract_ref": CONTRACT_REF,
        },
    )
    attached = tuple(
        {
            "snapshot_id": snapshot_id,
            "load_revision": load_revision,
            **row,
        }
        for row in rows
    )
    attached_hash = _streaming_rows_hash(attached)
    date_range = (min(business_dates), max(business_dates))
    source_range = (min(source_times), max(source_times))
    unchecked = PaymentOrderReconciliation(
        status="not_checked",
        source_rows=len(attached),
        paid_order_rows=0,
        matched_rows=0,
        missing_order_rows=0,
        unexpected_order_rows=0,
        user_mismatch_rows=0,
        business_date_mismatch_rows=0,
        paid_amount_mismatch_rows=0,
        source_paid_amount_ngn=paid_total,
        paid_order_amount_ngn=Decimal(0),
        difference_ngn=paid_total,
    )
    manifest = PaymentOrderBetLoadManifest(
        manifest_ref="pending",
        snapshot_ref="pending",
        release_ref="pending",
        snapshot_id=snapshot_id,
        load_revision=load_revision,
        dataset_id=DATASET_ID,
        physical_table=physical_table,
        watermark=date_range[1],
        row_count=len(attached),
        unique_order_count=len(order_ids),
        unique_user_count=len(user_ids),
        date_range=date_range,
        source_time_range=source_range,
        source_timezone=str(SOURCE_TIMEZONE),
        business_timezone=str(BUSINESS_TIMEZONE),
        schema_fields=tuple(name for name, _ in clean_schema),
        schema_fingerprint=fingerprint,
        rows_content_hash=attached_hash,
        source_checksums={path.name: checksum},
        contract_ref=CONTRACT_REF,
        runtime_binding_ref=RUNTIME_BINDING_REF,
        canonicalization_version=CANONICALIZATION_VERSION,
        quality=quality,
        reconciliation_ref="pending",
        reconciliation=unchecked,
    ).with_reconciliation(unchecked)
    return (
        PaymentOrderBetRows(
            snapshot_id=snapshot_id,
            load_revision=load_revision,
            rows=attached,
        ),
        manifest,
    )


def build_dataset_snapshot_payload(
    manifest: PaymentOrderBetLoadManifest,
) -> dict[str, Any]:
    if type(manifest) is not PaymentOrderBetLoadManifest:
        raise PaymentOrderBetLoadError("payment_order_bet_manifest_invalid")
    reconciliation = manifest.reconciliation
    if reconciliation.status == "not_checked":
        raise PaymentOrderBetLoadError("paid_order_reconciliation_required")
    if reconciliation.status != "matched":
        raise PaymentOrderBetLoadError("paid_order_reconciliation_failed")
    return {
        "snapshot_ref": manifest.snapshot_ref,
        "dataset_id": manifest.dataset_id,
        "physical_table": manifest.physical_table,
        "watermark": manifest.watermark,
        "schema_fingerprint": manifest.schema_fingerprint,
        "schema_fields": list(manifest.schema_fields),
        "contract_ref": manifest.contract_ref,
        "loaded_at": _snapshot_available_at(manifest.watermark),
        "status": "active",
        "evidence_state": "claim_ready",
        "reconciliation_status": reconciliation.status,
        "reconciliation_ref": manifest.reconciliation_ref,
        "logical_snapshot_id": manifest.snapshot_id,
        "load_revision": manifest.load_revision,
        "release_ref": manifest.release_ref,
        "rows_content_hash": manifest.rows_content_hash,
        "snapshot_id": manifest.snapshot_id,
        "source_load_manifest_ref": manifest.manifest_ref,
        "runtime_binding_ref": manifest.runtime_binding_ref,
        "source_checksums": dict(manifest.source_checksums),
        "row_count": manifest.row_count,
        "date_range": list(manifest.date_range),
        "no_data_partitions": [],
        "no_data_partition_windows": [],
        "requires_release": True,
        "reconciliation": json.loads(
            canonical_json_bytes(asdict(reconciliation)).decode("utf-8")
        ),
    }


def persist_dataset_snapshot_payload(
    store: Any,
    payload: Mapping[str, Any],
) -> SnapshotPersistenceResult:
    return persist_dataset_snapshot_payloads(store, (dict(payload),))


def apply_clickhouse_ddl(client: Any, manifest: PaymentOrderBetLoadManifest) -> None:
    _require_identifier(manifest.physical_table, "physical_table")
    contract = load_contract(SOURCE_CONTRACT_PATH)
    schema = _clean_schema(contract)
    columns = ",\n".join(f"`{name}` {data_type}" for name, data_type in schema)
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {manifest.physical_table}
        ({columns})
        ENGINE = {TABLE_ENGINE}
        ORDER BY ({', '.join(ORDER_BY)})
        """
    )
    validate_clickhouse_schema(client, manifest)


def validate_clickhouse_schema(
    client: Any,
    manifest: PaymentOrderBetLoadManifest,
) -> None:
    rows = tuple(
        dict(row)
        for row in client.query(
            """
            SELECT name, type
            FROM system.columns
            WHERE database = currentDatabase()
              AND table = {table:String}
            ORDER BY position
            """,
            parameters={"table": manifest.physical_table},
        ).named_results()
    )
    expected = _clean_schema(load_contract(SOURCE_CONTRACT_PATH))
    observed = tuple((str(row["name"]), str(row["type"])) for row in rows)
    if observed != expected:
        raise PaymentOrderBetLoadError("clickhouse_schema_drift:columns")


def stage_payment_order_bet_release(
    client: Any,
    rows: PaymentOrderBetRows,
    manifest: PaymentOrderBetLoadManifest,
    *,
    batch_size: int = 5000,
) -> str:
    if type(batch_size) is not int or batch_size < 1:
        raise PaymentOrderBetLoadError("invalid_insert_batch_size")
    existing = _persisted_count(client, manifest)
    if existing:
        if existing != manifest.row_count:
            raise PaymentOrderBetLoadError("staged_load_revision_invalid")
        validate_persisted_snapshot(client, manifest)
        return "already_validated"
    for start in range(0, len(rows.rows), batch_size):
        insert_json_each_row(
            client,
            manifest.physical_table,
            rows.rows[start : start + batch_size],
        )
    validate_persisted_snapshot(client, manifest)
    return "staged_and_validated"


def validate_persisted_snapshot(
    client: Any,
    manifest: PaymentOrderBetLoadManifest,
) -> Mapping[str, Any]:
    result = client.query(
        f"""
        SELECT
          count() AS row_count,
          uniqExact(order_id) AS unique_order_count,
          uniqExact(user_id) AS unique_user_count,
          toString(min(business_date_lagos)) AS min_business_date,
          toString(max(business_date_lagos)) AS max_business_date,
          countIf(paid_amount_ngn <= 0 OR bet_24h_amount_ngn < 0 OR bet_7d_amount_ngn < 0) AS invalid_amount_rows,
          countIf(bet_24h_amount_ngn > bet_7d_amount_ngn) AS invalid_window_rows,
          countIf((played_within_24h = 1) != (bet_24h_amount_ngn > 0)) AS invalid_flag_rows,
          sum(paid_amount_ngn) AS paid_amount_total_ngn,
          sum(bet_24h_amount_ngn) AS bet_24h_amount_total_ngn,
          sum(bet_7d_amount_ngn) AS bet_7d_amount_total_ngn
        FROM {manifest.physical_table}
        WHERE snapshot_id = {{snapshot_id:String}}
          AND load_revision = {{load_revision:String}}
        """,
        parameters={
            "snapshot_id": manifest.snapshot_id,
            "load_revision": manifest.load_revision,
        },
    )
    records = tuple(dict(row) for row in result.named_results())
    if len(records) != 1:
        raise PaymentOrderBetLoadError("persisted_validation_shape_invalid")
    record = records[0]
    if (
        int(record["row_count"]) != manifest.row_count
        or int(record["unique_order_count"]) != manifest.unique_order_count
        or int(record["unique_user_count"]) != manifest.unique_user_count
        or (str(record["min_business_date"]), str(record["max_business_date"]))
        != manifest.date_range
        or int(record["invalid_amount_rows"]) != 0
        or int(record["invalid_window_rows"]) != 0
        or int(record["invalid_flag_rows"]) != 0
        or _decimal(record["paid_amount_total_ngn"], "persisted_paid_amount")
        != manifest.quality["paid_amount_ngn"]
        or _decimal(record["bet_24h_amount_total_ngn"], "persisted_bet_24h_amount")
        != manifest.quality["bet_24h_amount_ngn"]
        or _decimal(record["bet_7d_amount_total_ngn"], "persisted_bet_7d_amount")
        != manifest.quality["bet_7d_amount_ngn"]
    ):
        raise PaymentOrderBetLoadError("persisted_snapshot_validation_failed")
    return record


def reconcile_payment_order_bet_link(
    client: Any,
    manifest: PaymentOrderBetLoadManifest,
    *,
    paid_order_table: str = "paid_order_success_clean_20240101_20260704_v2",
) -> PaymentOrderReconciliation:
    _require_identifier(paid_order_table, "paid_order_table")
    result = client.query(
        f"""
        SELECT
          countIf(s.order_id IS NOT NULL) AS source_rows,
          countIf(p.order_id IS NOT NULL) AS paid_order_rows,
          countIf(s.order_id IS NOT NULL AND p.order_id IS NOT NULL) AS matched_rows,
          countIf(s.order_id IS NOT NULL AND p.order_id IS NULL) AS missing_order_rows,
          countIf(s.order_id IS NULL AND p.order_id IS NOT NULL) AS unexpected_order_rows,
          countIf(s.order_id IS NOT NULL AND p.order_id IS NOT NULL AND s.user_id != p.user_id) AS user_mismatch_rows,
          countIf(s.order_id IS NOT NULL AND p.order_id IS NOT NULL AND s.business_date_lagos != p.business_date_lagos) AS business_date_mismatch_rows,
          countIf(s.order_id IS NOT NULL AND p.order_id IS NOT NULL AND abs(toFloat64(s.paid_amount_ngn) - p.paid_amount_ngn) > 0.0001) AS paid_amount_mismatch_rows,
          sumIf(s.paid_amount_ngn, s.order_id IS NOT NULL) AS source_paid_amount_ngn,
          sumIf(toDecimal128(p.paid_amount_ngn, 4), p.order_id IS NOT NULL) AS paid_order_amount_ngn
        FROM
        (
          SELECT order_id, user_id, business_date_lagos, paid_amount_ngn
          FROM {manifest.physical_table}
          WHERE snapshot_id = {{snapshot_id:String}}
            AND load_revision = {{load_revision:String}}
        ) AS s
        FULL OUTER JOIN
        (
          SELECT order_id, user_id, business_date_lagos, paid_amount_ngn
          FROM {paid_order_table}
          WHERE business_date_lagos BETWEEN {{start:Date}} AND {{end:Date}}
        ) AS p USING order_id
        SETTINGS join_use_nulls = 1
        """,
        parameters={
            "snapshot_id": manifest.snapshot_id,
            "load_revision": manifest.load_revision,
            "start": manifest.date_range[0],
            "end": manifest.date_range[1],
        },
    )
    records = tuple(dict(row) for row in result.named_results())
    if len(records) != 1:
        raise PaymentOrderBetLoadError("paid_order_reconciliation_shape_invalid")
    row = records[0]
    source_amount = _decimal(row["source_paid_amount_ngn"], "source_paid_amount")
    paid_amount = _decimal(row["paid_order_amount_ngn"], "paid_order_amount")
    values = {
        key: int(row[key])
        for key in (
            "source_rows",
            "paid_order_rows",
            "matched_rows",
            "missing_order_rows",
            "unexpected_order_rows",
            "user_mismatch_rows",
            "business_date_mismatch_rows",
            "paid_amount_mismatch_rows",
        )
    }
    matched = (
        values["source_rows"] == manifest.row_count
        and values["paid_order_rows"] == manifest.row_count
        and values["matched_rows"] == manifest.row_count
        and all(
            values[key] == 0
            for key in (
                "missing_order_rows",
                "unexpected_order_rows",
                "user_mismatch_rows",
                "business_date_mismatch_rows",
                "paid_amount_mismatch_rows",
            )
        )
        and source_amount == paid_amount
    )
    return PaymentOrderReconciliation(
        status="matched" if matched else "mismatch",
        source_paid_amount_ngn=source_amount,
        paid_order_amount_ngn=paid_amount,
        difference_ngn=source_amount - paid_amount,
        **values,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--manifest-out", type=Path, required=True)
    parser.add_argument("--clickhouse-container", default="")
    parser.add_argument("--skip-postgres", action="store_true")
    args = parser.parse_args(argv)

    rows, manifest = load_payment_order_bet_rows(
        args.source,
        snapshot_id=args.snapshot_id,
    )
    if args.clickhouse_container:
        database = os.environ.get("WAJE_CLICKHOUSE_DATABASE", "")
        if not database:
            raise PaymentOrderBetLoadError("missing_clickhouse_database")
        client = DockerClickHouseClient(args.clickhouse_container, database)
    else:
        runtime = ClickHouseRuntime.from_env()
        if not runtime.configured():
            raise PaymentOrderBetLoadError(
                "missing_clickhouse_binding:" + ",".join(runtime.binding.missing)
            )
        client = runtime._get_client()
    apply_clickhouse_ddl(client, manifest)
    stage_payment_order_bet_release(client, rows, manifest)
    reconciliation = reconcile_payment_order_bet_link(client, manifest)
    manifest = manifest.with_reconciliation(reconciliation)
    payload = build_dataset_snapshot_payload(manifest)
    persistence = SnapshotPersistenceResult(active_refs=(), superseded_refs=())
    store = None if args.skip_postgres else PostgresConversationStore.from_env()
    try:
        if store is not None:
            with store.dataset_snapshot_release_lock(manifest.snapshot_id):
                persistence = persist_dataset_snapshot_payload(store, payload)
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
                "release_ref": manifest.release_ref,
                "snapshot_ref": manifest.snapshot_ref,
                "row_count": manifest.row_count,
                "date_range": manifest.date_range,
                "reconciliation_status": reconciliation.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _normalize_row(
    raw: Mapping[str, str],
    *,
    mapping: Mapping[str, str],
    row_number: int,
) -> dict[str, Any]:
    order_id = _text(raw[mapping["order_id"]], "order_id", row_number)
    user_id = _text(raw[mapping["user_id"]], "user_id", row_number)
    source_time = _timestamp(
        raw[mapping["payment_completed_at_source"]],
        "payment_completed_at_source",
        row_number,
    )
    deadline = _timestamp(
        raw[mapping["payment_24h_deadline_source"]],
        "payment_24h_deadline_source",
        row_number,
    )
    if deadline - source_time != timedelta(hours=24):
        raise PaymentOrderBetLoadError(
            f"payment_24h_deadline_mismatch:row={row_number}"
        )
    paid = _amount(raw[mapping["paid_amount_ngn"]], "paid_amount_ngn", row_number)
    bet_24h = _amount(
        raw[mapping["bet_24h_amount_ngn"]],
        "bet_24h_amount_ngn",
        row_number,
        allow_zero=True,
    )
    bet_7d = _amount(
        raw[mapping["bet_7d_amount_ngn"]],
        "bet_7d_amount_ngn",
        row_number,
        allow_zero=True,
    )
    if bet_24h > bet_7d:
        raise PaymentOrderBetLoadError(
            f"bet_window_monotonicity_invalid:row={row_number}"
        )
    flag = str(raw[mapping["played_within_24h"]]).strip()
    if flag not in {"是", "否"}:
        raise PaymentOrderBetLoadError(f"played_flag_invalid:row={row_number}")
    played = 1 if flag == "是" else 0
    if (played == 1) != (bet_24h > 0):
        raise PaymentOrderBetLoadError(
            f"played_flag_amount_mismatch:row={row_number}"
        )
    ratio_24h = _ratio(
        raw[mapping["reported_bet_to_paid_ratio_24h"]],
        "reported_bet_to_paid_ratio_24h",
        row_number,
    )
    ratio_7d = _ratio(
        raw[mapping["reported_bet_to_paid_ratio_7d"]],
        "reported_bet_to_paid_ratio_7d",
        row_number,
    )
    if abs((bet_24h / paid) - ratio_24h) > RATIO_TOLERANCE or abs(
        (bet_7d / paid) - ratio_7d
    ) > RATIO_TOLERANCE:
        raise PaymentOrderBetLoadError(
            f"reported_ratio_rounding_mismatch:row={row_number}"
        )
    share_24h = str(raw[mapping["gameplay_share_24h"]]).strip()
    share_7d = str(raw[mapping["gameplay_share_7d"]]).strip()
    _validate_gameplay_share(share_24h, bet_24h, "gameplay_share_24h", row_number)
    _validate_gameplay_share(share_7d, bet_7d, "gameplay_share_7d", row_number)
    lagos = source_time.replace(tzinfo=SOURCE_TIMEZONE).astimezone(BUSINESS_TIMEZONE)
    return {
        "order_id": order_id,
        "user_id": user_id,
        "business_date_lagos": lagos.date().isoformat(),
        "payment_completed_at_lagos": lagos.strftime("%Y-%m-%d %H:%M:%S.000"),
        "paid_amount_ngn": paid.quantize(AMOUNT_QUANTUM),
        "played_within_24h": played,
        "bet_24h_amount_ngn": bet_24h.quantize(AMOUNT_QUANTUM),
        "bet_7d_amount_ngn": bet_7d.quantize(AMOUNT_QUANTUM),
        "reported_bet_to_paid_ratio_24h": ratio_24h.quantize(RATIO_QUANTUM),
        "reported_bet_to_paid_ratio_7d": ratio_7d.quantize(RATIO_QUANTUM),
        "gameplay_share_24h": share_24h,
        "gameplay_share_7d": share_7d,
    }


def _validate_gameplay_share(
    value: str,
    bet_amount: Decimal,
    field: str,
    row_number: int,
) -> None:
    if not value:
        if bet_amount != 0:
            raise PaymentOrderBetLoadError(
                f"gameplay_share_missing_for_positive_bet:{field}:row={row_number}"
            )
        return
    if bet_amount == 0:
        raise PaymentOrderBetLoadError(
            f"gameplay_share_present_for_zero_bet:{field}:row={row_number}"
        )
    for part in value.split(" | "):
        if ":" not in part or not part.endswith("%"):
            raise PaymentOrderBetLoadError(
                f"gameplay_share_invalid:{field}:row={row_number}"
            )
        game, percentage = part.rsplit(":", 1)
        if not game.strip():
            raise PaymentOrderBetLoadError(
                f"gameplay_share_invalid:{field}:row={row_number}"
            )
        parsed = _decimal(percentage[:-1], f"{field}:percentage")
        if parsed < 0 or parsed > 100:
            raise PaymentOrderBetLoadError(
                f"gameplay_share_invalid:{field}:row={row_number}"
            )


def _gameplay_share_rounding_outlier(value: str) -> bool:
    if not value:
        return False
    total = sum(
        (_decimal(part.rsplit(":", 1)[1][:-1], "gameplay_share_percentage") for part in value.split(" | ")),
        Decimal(0),
    )
    return abs(total - Decimal(100)) > Decimal("0.11")


def _field_mapping(contract: Mapping[str, Any]) -> dict[str, str]:
    raw = _mapping(contract.get("field_mapping"), "source_field_mapping_missing")
    required = {
        "order_id",
        "user_id",
        "payment_completed_at_source",
        "paid_amount_ngn",
        "payment_24h_deadline_source",
        "played_within_24h",
        "bet_24h_amount_ngn",
        "reported_bet_to_paid_ratio_24h",
        "gameplay_share_24h",
        "bet_7d_amount_ngn",
        "reported_bet_to_paid_ratio_7d",
        "gameplay_share_7d",
    }
    mapping = {str(key): str(value) for key, value in raw.items()}
    if set(mapping) != required or len(set(mapping.values())) != len(mapping):
        raise PaymentOrderBetLoadError("source_field_mapping_invalid")
    return mapping


def _clean_schema(contract: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    storage = _mapping(contract.get("storage_boundary"), "storage_boundary_invalid")
    raw = storage.get("clean_schema")
    if not isinstance(raw, list) or not raw:
        raise PaymentOrderBetLoadError("clean_schema_invalid")
    schema = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"name", "type"}:
            raise PaymentOrderBetLoadError("clean_schema_invalid")
        name, data_type = str(item["name"]), str(item["type"])
        _require_identifier(name, "schema_field")
        if not data_type:
            raise PaymentOrderBetLoadError("clean_schema_invalid")
        schema.append((name, data_type))
    if tuple(name for name, _ in schema) != (
        "snapshot_id",
        "load_revision",
        "order_id",
        "user_id",
        "business_date_lagos",
        "payment_completed_at_lagos",
        "paid_amount_ngn",
        "played_within_24h",
        "bet_24h_amount_ngn",
        "bet_7d_amount_ngn",
        "reported_bet_to_paid_ratio_24h",
        "reported_bet_to_paid_ratio_7d",
        "gameplay_share_24h",
        "gameplay_share_7d",
    ):
        raise PaymentOrderBetLoadError("clean_schema_invalid")
    return tuple(schema)


def _schema_descriptor(schema: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    return (
        *(f"{name}:{data_type}" for name, data_type in schema),
        f"engine:{TABLE_ENGINE}",
        "order_by:" + ",".join(ORDER_BY),
        f"canonicalization:{CANONICALIZATION_VERSION}",
    )


def _validate_headers(
    actual: Sequence[str] | None,
    expected: Sequence[str],
) -> None:
    if tuple(actual or ()) != tuple(expected):
        raise PaymentOrderBetLoadError("source_header_mismatch")


def _text(value: Any, field: str, row_number: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise PaymentOrderBetLoadError(f"source_text_missing:{field}:row={row_number}")
    return normalized


def _timestamp(value: Any, field: str, row_number: int) -> datetime:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise PaymentOrderBetLoadError(
            f"source_timestamp_invalid:{field}:row={row_number}"
        ) from exc


def _amount(
    value: Any,
    field: str,
    row_number: int,
    *,
    allow_zero: bool = False,
) -> Decimal:
    parsed = _decimal(value, field)
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise PaymentOrderBetLoadError(
            f"source_amount_invalid:{field}:row={row_number}"
        )
    return parsed


def _ratio(value: Any, field: str, row_number: int) -> Decimal:
    parsed = _decimal(value, field)
    if parsed < 0:
        raise PaymentOrderBetLoadError(
            f"source_ratio_invalid:{field}:row={row_number}"
        )
    return parsed


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaymentOrderBetLoadError(f"source_decimal_invalid:{field}") from exc
    if not parsed.is_finite():
        raise PaymentOrderBetLoadError(f"source_decimal_invalid:{field}")
    return parsed


def _mapping(value: Any, error: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PaymentOrderBetLoadError(error)
    return value


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PaymentOrderBetLoadError(f"invalid_identifier:{field}")


def _versioned_table_name(prefix: str, fingerprint: str) -> str:
    _require_identifier(prefix, "physical_table_prefix")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise PaymentOrderBetLoadError("invalid_schema_fingerprint")
    return f"{prefix}__{fingerprint[:16]}"


def _streaming_rows_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_json_bytes(row))
        digest.update(b"\n")
    return digest.hexdigest()


def _snapshot_ref(
    *,
    manifest_ref: str,
    snapshot_id: str,
    load_revision: str,
    physical_table: str,
    watermark: str,
    fingerprint: str,
) -> str:
    return content_ref(
        "dataset-snapshot",
        {
            "manifest_ref": manifest_ref,
            "snapshot_id": snapshot_id,
            "dataset_id": DATASET_ID,
            "load_revision": load_revision,
            "physical_table": physical_table,
            "watermark": watermark,
            "schema_fingerprint": fingerprint,
        },
    )


def _snapshot_available_at(watermark: str) -> str:
    return datetime.combine(
        date.fromisoformat(watermark) + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    ).isoformat()


def _persisted_count(client: Any, manifest: PaymentOrderBetLoadManifest) -> int:
    result = client.query(
        f"""
        SELECT count() AS row_count
        FROM {manifest.physical_table}
        WHERE snapshot_id = {{snapshot_id:String}}
          AND load_revision = {{load_revision:String}}
        """,
        parameters={
            "snapshot_id": manifest.snapshot_id,
            "load_revision": manifest.load_revision,
        },
    )
    rows = tuple(dict(row) for row in result.named_results())
    return int(rows[0]["row_count"]) if len(rows) == 1 else -1


if __name__ == "__main__":
    raise SystemExit(main())
