#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
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
from bi_agent.runtime.dataset_catalog import (
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
    validate_dataset_snapshot_release_payloads,
)
from tools.data.source_loader_common import (
    canonical_json_bytes,
    file_sha256,
    schema_fingerprint,
)


SOURCE_CONTRACT_PATH = ROOT / "contracts" / "sources" / "paid-order-detail.source.yaml"
DATASET_ID = "paid_order_success"
CONTRACT_REF = "contracts/sources/paid-order-detail.source.yaml@0.3"
RUNTIME_BINDING_REF = "contracts/runtime/clickhouse-analysis-bindings.yaml@1"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PaidSuccessRegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class ExistingPaidSuccessInspection:
    archive_sha256: str
    physical_table: str
    schema_fields: tuple[str, ...]
    schema_fingerprint: str
    row_count: int
    date_range: tuple[str, str]
    watermark: str
    rows_content_hash: str
    source_checksums: tuple[tuple[str, str], ...]
    validation_errors: tuple[str, ...]

    @property
    def ready_to_publish(self) -> bool:
        return not self.validation_errors


@dataclass(frozen=True)
class PaidSuccessRegistrationResult:
    release_ref: str
    snapshot_refs: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    authority_record_ref: str


def inspect_existing_paid_success(
    client: Any,
    *,
    archive_path: Path,
    physical_table: str,
    source_contract: Mapping[str, Any],
) -> ExistingPaidSuccessInspection:
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise PaidSuccessRegistrationError(f"archive_missing:{archive_path}")
    _require_identifier(physical_table, "physical_table")
    archive_sha256 = file_sha256(archive_path)
    errors: list[str] = []

    source_file = _mapping(source_contract.get("source_file"))
    expected_archive_sha256 = str(source_file.get("sha256") or "")
    if not expected_archive_sha256:
        errors.append("archive_checksum:reviewed_contract_value_missing")
    elif archive_sha256 != expected_archive_sha256:
        errors.append(
            f"archive_checksum:mismatch:expected={expected_archive_sha256}:actual={archive_sha256}"
        )

    storage = _mapping(source_contract.get("storage_boundary"))
    analytical_database = str(storage.get("analytical_database") or "")
    _require_identifier(analytical_database, "analytical_database")
    configured_database_rows = _query_rows(
        client,
        "SELECT currentDatabase() AS configured_database",
    )
    if (
        len(configured_database_rows) != 1
        or str(configured_database_rows[0].get("configured_database") or "")
        != analytical_database
    ):
        raise PaidSuccessRegistrationError("analytical_database:mismatch")
    expected_table = str(storage.get("clean_table") or "")
    if physical_table != expected_table:
        errors.append(
            f"physical_table:mismatch:expected={expected_table}:actual={physical_table}"
        )
    expected_schema = _reviewed_schema(storage.get("clean_schema"), errors)

    schema_rows = _query_rows(
        client,
        """
        SELECT name, type
        FROM system.columns
        WHERE database = {analytical_database:String}
          AND table = {physical_table:String}
        ORDER BY position
        """,
        parameters={
            "analytical_database": analytical_database,
            "physical_table": physical_table,
        },
    )
    actual_schema = tuple(
        (str(item.get("name") or ""), str(item.get("type") or ""))
        for item in schema_rows
    )
    if actual_schema != expected_schema:
        errors.append(
            "schema:mismatch:expected="
            + _compact(expected_schema)
            + ":actual="
            + _compact(actual_schema)
        )
    if errors:
        raise PaidSuccessRegistrationError(";".join(errors))

    boundary = _mapping(source_contract.get("paid_amount_boundary"))
    _validate_success_semantics(boundary, errors)
    expected_profile = _mapping(boundary.get("cleaned_profile"))
    expected_count = _integer(expected_profile.get("paid_records"), default=-1)
    date_profile = _mapping(source_file.get("date_range"))
    expected_range = (
        str(date_profile.get("start") or ""),
        str(date_profile.get("end") or ""),
    )

    fingerprint_expression = ", ".join(
        _fingerprint_column_expression(name, data_type)
        for name, data_type in expected_schema
    )
    aggregate_rows = _query_rows(
        client,
        f"""
        SELECT
          count() AS row_count,
          toString(min(business_date_lagos)) AS min_business_date,
          toString(max(business_date_lagos)) AS max_business_date,
          countIf(empty(order_id) OR empty(user_id) OR isNull(business_date_lagos)) AS null_critical_fields,
          countIf(NOT isFinite(paid_amount_ngn) OR paid_amount_ngn <= 0) AS invalid_amount_rows,
          count() - uniqExact(order_id) AS duplicate_key_rows,
          groupBitXor(cityHash64(tuple({fingerprint_expression}))) AS content_hash_a,
          groupBitXor(cityHash64(tuple({fingerprint_expression}), 1)) AS content_hash_b
        FROM {_qualified_table_identifier(analytical_database, physical_table)}
        """,
    )
    if len(aggregate_rows) != 1:
        errors.append(f"aggregate_fingerprint:row_shape={len(aggregate_rows)}")
        aggregate: Mapping[str, Any] = {}
    else:
        aggregate = aggregate_rows[0]
    row_count = _integer(aggregate.get("row_count"), default=-1)
    date_range = (
        str(aggregate.get("min_business_date") or ""),
        str(aggregate.get("max_business_date") or ""),
    )
    if row_count != expected_count:
        errors.append(f"row_count:mismatch:expected={expected_count}:actual={row_count}")
    if date_range != expected_range:
        errors.append(
            f"date_range:mismatch:expected={_compact(expected_range)}:actual={_compact(date_range)}"
        )
    for field, failure_type in (
        ("null_critical_fields", "critical_fields"),
        ("invalid_amount_rows", "amount_bounds"),
        ("duplicate_key_rows", "duplicate_key"),
    ):
        count = _integer(aggregate.get(field), default=-1)
        if count != 0:
            errors.append(f"{failure_type}:invalid_rows={count}")

    rows_hash_payload = {
        "algorithm": "clickhouse-aggregate-cityhash64-xor-null-marker-v2",
        "schema": actual_schema,
        "row_count": row_count,
        "content_hash_a": str(aggregate.get("content_hash_a") or ""),
        "content_hash_b": str(aggregate.get("content_hash_b") or ""),
    }
    inspection = ExistingPaidSuccessInspection(
        archive_sha256=archive_sha256,
        physical_table=physical_table,
        schema_fields=tuple(name for name, _ in actual_schema),
        schema_fingerprint=schema_fingerprint(
            tuple(f"{name}:{data_type}" for name, data_type in actual_schema)
        ),
        row_count=row_count,
        date_range=date_range,
        watermark=date_range[1],
        rows_content_hash=hashlib.sha256(
            canonical_json_bytes(rows_hash_payload)
        ).hexdigest(),
        source_checksums=(("archive_sha256", archive_sha256),),
        validation_errors=tuple(errors),
    )
    if errors:
        raise PaidSuccessRegistrationError(";".join(errors))
    return inspection


def build_paid_success_snapshot_payload(
    inspection: ExistingPaidSuccessInspection,
    *,
    snapshot_id: str,
    load_revision: str,
    loaded_at: str,
) -> dict[str, Any]:
    if not inspection.ready_to_publish:
        raise PaidSuccessRegistrationError(";".join(inspection.validation_errors))
    snapshot_ref = f"snapshot:{snapshot_id}:{load_revision}:{DATASET_ID}"
    release_ref = dataset_snapshot_release_ref(snapshot_id, load_revision, (snapshot_ref,))
    return {
        "snapshot_ref": snapshot_ref,
        "dataset_id": DATASET_ID,
        "physical_table": inspection.physical_table,
        "watermark": inspection.watermark,
        "schema_fingerprint": inspection.schema_fingerprint,
        "schema_fields": list(inspection.schema_fields),
        "contract_ref": CONTRACT_REF,
        "permission_scopes": ["analyst"],
        "loaded_at": loaded_at,
        "status": "active",
        "evidence_state": "claim_ready",
        "reconciliation_status": "not_applicable",
        "reconciliation_ref": "",
        "logical_snapshot_id": snapshot_id,
        "load_revision": load_revision,
        "release_ref": release_ref,
        "rows_content_hash": inspection.rows_content_hash,
        "snapshot_id": snapshot_id,
        "source_load_manifest_ref": f"source-load:{snapshot_id}:{load_revision}",
        "runtime_binding_ref": RUNTIME_BINDING_REF,
        "source_checksums": dict(inspection.source_checksums),
        "row_count": inspection.row_count,
        "date_range": list(inspection.date_range),
        "no_data_partitions": [],
        "no_data_partition_windows": [],
    }


def register_existing_paid_success_snapshot(
    store: Any,
    inspection: ExistingPaidSuccessInspection,
    *,
    snapshot_id: str,
    load_revision: str,
    loaded_at: str,
) -> PaidSuccessRegistrationResult:
    with store.dataset_snapshot_release_lock(snapshot_id):
        payload = build_paid_success_snapshot_payload(
            inspection,
            snapshot_id=snapshot_id,
            load_revision=load_revision,
            loaded_at=loaded_at,
        )
        payloads, logical_id, _, release_ref = validate_dataset_snapshot_release_payloads(
            (payload,)
        )
        authority = build_dataset_release_authority_record(payloads)
        if authority.integrity_errors:
            raise PaidSuccessRegistrationError(
                "release_authority:" + ",".join(authority.integrity_errors)
            )
        store.publish_dataset_snapshot_release(
            release_ref=release_ref,
            logical_snapshot_id=logical_id,
            payloads=payloads,
        )
    persisted = store.resolve_dataset_release(release_ref)
    return PaidSuccessRegistrationResult(
        release_ref=release_ref,
        snapshot_refs=persisted.snapshot_refs,
        dataset_ids=persisted.dataset_ids,
        authority_record_ref=persisted.authority_record_ref,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--physical-table", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--load-revision", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)

    try:
        contract = load_contract(SOURCE_CONTRACT_PATH)
        runtime = ClickHouseRuntime.from_env()
        if not runtime.configured():
            raise PaidSuccessRegistrationError(
                "clickhouse_binding:" + ",".join(runtime.binding.missing)
            )
        inspection = inspect_existing_paid_success(
            runtime._get_client(),
            archive_path=args.archive,
            physical_table=args.physical_table,
            source_contract=contract,
        )
        output: dict[str, Any] = _inspection_metadata(inspection)
        if args.publish:
            if not os.environ.get("WAJE_RUNTIME_DATABASE_URL"):
                raise PaidSuccessRegistrationError(
                    "postgres_binding:WAJE_RUNTIME_DATABASE_URL"
                )
            accepted_on = str(
                _mapping(contract.get("owner_acceptance")).get("accepted_on") or ""
            )
            loaded_at = accepted_on + "T00:00:00+00:00"
            store = PostgresConversationStore.from_env()
            try:
                result = register_existing_paid_success_snapshot(
                    store,
                    inspection,
                    snapshot_id=args.snapshot_id,
                    load_revision=args.load_revision,
                    loaded_at=loaded_at,
                )
            finally:
                close = getattr(store.connection, "close", None)
                if callable(close):
                    close()
            output["registration"] = {
                "release_ref": result.release_ref,
                "snapshot_refs": result.snapshot_refs,
                "dataset_ids": result.dataset_ids,
                "authority_record_ref": result.authority_record_ref,
            }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        if isinstance(exc, PaidSuccessRegistrationError):
            error_code = "paid_success_validation_failed"
            validation_errors = str(exc).split(";")
        else:
            error_code = "registration_runtime_failed"
            validation_errors = [error_code]
        print(
            json.dumps(
                {
                    "ready_to_publish": False,
                    "error_code": error_code,
                    "validation_errors": validation_errors,
                    "owner": "payment_contract_owner",
                    "impact": "paid_order_success authority release withheld",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


def _inspection_metadata(inspection: ExistingPaidSuccessInspection) -> dict[str, Any]:
    return {
        "ready_to_publish": inspection.ready_to_publish,
        "physical_table": inspection.physical_table,
        "schema_fields": inspection.schema_fields,
        "schema_fingerprint": inspection.schema_fingerprint,
        "row_count": inspection.row_count,
        "date_range": inspection.date_range,
        "watermark": inspection.watermark,
        "rows_content_hash": inspection.rows_content_hash,
        "source_checksums": dict(inspection.source_checksums),
        "validation_errors": inspection.validation_errors,
        "dataset_ids": (DATASET_ID,),
    }


def _query_rows(client: Any, query: str, *, parameters=None) -> tuple[Mapping[str, Any], ...]:
    result = client.query(query, parameters=parameters or {})
    return tuple(dict(item) for item in result.named_results())


def _reviewed_schema(value: Any, errors: list[str]) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, (tuple, list)) or not value:
        errors.append("schema:reviewed_contract_value_missing")
        return ()
    result: list[tuple[str, str]] = []
    for item in value:
        mapping = _mapping(item)
        name, data_type = str(mapping.get("name") or ""), str(mapping.get("type") or "")
        if not _IDENTIFIER.fullmatch(name) or not data_type:
            errors.append("schema:reviewed_contract_value_invalid")
            return ()
        result.append((name, data_type))
    return tuple(result)


def _validate_success_semantics(boundary: Mapping[str, Any], errors: list[str]) -> None:
    expected = {
        "final_success_status": "pay_success",
        "business_date_basis": "支付完成时间 converted to Africa/Lagos",
        "dedup_key": "订单id",
        "duplicate_success_rule": "keep latest 支付完成时间",
    }
    for field, value in expected.items():
        actual = str(boundary.get(field) or "")
        if actual != value:
            errors.append(f"success_semantics:{field}:expected={value}:actual={actual}")


def _fingerprint_column_expression(name: str, data_type: str) -> str:
    column = _quote_identifier(name, "schema_field")
    nullable = re.fullmatch(r"Nullable\((.+)\)", data_type)
    if nullable is None:
        return column
    nested = nullable.group(1)
    if nested == "String" or nested.startswith("FixedString("):
        default = "''"
    elif nested == "Date":
        default = "toDate(0)"
    elif nested.startswith("DateTime"):
        default = "toDateTime(0)"
    else:
        default = "0"
    return f"tuple(isNull({column}), ifNull({column}, {default}))"


def _require_identifier(value: str, field: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise PaidSuccessRegistrationError(f"{field}:invalid_identifier")


def _quote_identifier(value: str, field: str) -> str:
    _require_identifier(value, field)
    return f"`{value}`"


def _qualified_table_identifier(database: str, table: str) -> str:
    return (
        _quote_identifier(database, "analytical_database")
        + "."
        + _quote_identifier(table, "physical_table")
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
