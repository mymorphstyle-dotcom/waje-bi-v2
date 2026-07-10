from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ReconciliationObservation:
    business_date: str
    game: str
    overall_paid_amount: Decimal | None
    channel_paid_amount: Decimal | None
    difference: Decimal | None
    status: str


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    reasons: tuple[str, ...]
    compared_dates: tuple[str, ...]
    observations: tuple[ReconciliationObservation, ...]
    tolerance: float


@dataclass(frozen=True)
class SourceLoadManifest:
    manifest_ref: str
    snapshot_ref: str
    snapshot_id: str
    load_revision: str
    release_ref: str
    dataset_id: str
    physical_table: str
    channel_dataset_id: str
    channel_physical_table: str
    watermark: str
    channel_watermark: str
    overall_source_row_count: int
    channel_source_row_count: int
    row_count: int
    channel_row_count: int
    date_range: tuple[str, str]
    channel_date_range: tuple[str, str] | tuple[()]
    schema_fields: tuple[str, ...]
    channel_schema_fields: tuple[str, ...]
    schema_fingerprint: str
    channel_schema_fingerprint: str
    overall_rows_content_hash: str
    channel_rows_content_hash: str
    source_checksums: Mapping[str, str]
    no_data_partitions: tuple[str, ...]
    no_data_partition_windows: tuple[str, ...]
    contract_ref: str
    runtime_binding_ref: str
    canonicalization_version: str
    reconciliation_ref: str
    reconciliation: ReconciliationResult

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def schema_fingerprint(fields: Sequence[str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes({"fields": tuple(str(field) for field in fields)})
    ).hexdigest()


def content_ref(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}:sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def rows_content_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(tuple(dict(row) for row in rows))).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical_decimal_not_finite")
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def insert_json_each_row(
    client: Any,
    table: str,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if not rows:
        return
    column_names = tuple(rows[0])
    if any(tuple(row) != column_names for row in rows):
        raise ValueError(f"inconsistent_insert_columns:{table}")
    block = b"\n".join(canonical_json_bytes(dict(row)) for row in rows) + b"\n"
    client.raw_insert(
        table,
        column_names=column_names,
        insert_block=block,
        settings={"async_insert": 0},
        fmt="JSONEachRow",
    )
