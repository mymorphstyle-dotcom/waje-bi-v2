#!/usr/bin/env python3
from __future__ import annotations

# This executable bootstraps the repository root before importing project modules.
# ruff: noqa: E402

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.dataset_catalog import (
    dataset_release_authority_integrity_errors,
    dataset_snapshot_release_ref,
    validate_dataset_snapshot_release_payloads,
)
from tools.data.source_loader_common import (
    ReconciliationObservation,
    ReconciliationResult,
    SourceLoadManifest,
    canonical_json_bytes,
    content_ref,
    file_sha256,
    insert_json_each_row,
    rows_content_hash,
    schema_fingerprint,
)


SOURCE_CONTRACT_PATH = ROOT / "contracts" / "sources" / "market-dashboard.source.yaml"
RUNTIME_BINDING_PATH = (
    ROOT / "contracts" / "runtime" / "clickhouse-analysis-bindings.yaml"
)
DDL_PATH = ROOT / "tools" / "data" / "clickhouse-analysis-sources.sql"
OVERALL_TABLE = "market_dashboard_daily"
CHANNEL_TABLE = "market_dashboard_channel_daily"
OVERALL_DATASET_ID = "market_dashboard"
CHANNEL_DATASET_ID = "market_dashboard_channel"
CONTRACT_REF = "contracts/sources/market-dashboard.source.yaml@0.1"
RUNTIME_BINDING_REF = "contracts/runtime/clickhouse-analysis-bindings.yaml@15"
CHANNEL_FILENAME_PATTERN = re.compile(
    r"^(?P<channel>.+)_(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.csv$"
)
OVERALL_FILENAME_PATTERN = re.compile(
    r"^.+_(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.csv$"
)
RECONCILIATION_TOLERANCE = 0.01
CANONICALIZATION_VERSION = "market-dashboard-decimal-v1"
TABLE_ENGINE = "MergeTree"
OVERALL_ORDER_BY = ("snapshot_id", "load_revision", "business_date", "game")
CHANNEL_ORDER_BY = (*OVERALL_ORDER_BY, "channel")


class DashboardLoadError(ValueError):
    pass


@dataclass(frozen=True)
class MarketDashboardRows:
    snapshot_id: str
    load_revision: str
    overall_rows: tuple[Mapping[str, Any], ...]
    channel_rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class PersistedSnapshotValidation:
    status: str
    overall_row_count: int
    channel_row_count: int
    overall_date_range: tuple[str, str]
    channel_date_range: tuple[str, str] | tuple[()]
    reconciliation: ReconciliationResult


@dataclass(frozen=True)
class SnapshotPersistenceResult:
    active_refs: tuple[str, ...]
    superseded_refs: tuple[str, ...]
    verified_payloads: tuple[Mapping[str, Any], ...] = ()
    authority_record: Mapping[str, Any] | None = None


class DockerClickHouseClient:
    """Explicit local ingestion control-plane client; never used by analysis runtime."""

    def __init__(self, container: str, database: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise DashboardLoadError("invalid_clickhouse_container")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise DashboardLoadError("invalid_clickhouse_database")
        self.container = container
        self.database = database

    def command(self, query, parameters=None, settings=None):
        return self._run(query, parameters=parameters, settings=settings).strip()

    def raw_insert(
        self,
        table,
        *,
        column_names=None,
        insert_block=None,
        settings=None,
        fmt=None,
    ):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise DashboardLoadError("invalid_clickhouse_table")
        if fmt != "JSONEachRow":
            raise DashboardLoadError("unsupported_clickhouse_insert_format")
        return self._run(
            f"INSERT INTO {table} FORMAT JSONEachRow",
            data=bytes(insert_block or b""),
            settings=settings,
        )

    def query(self, query, parameters=None, settings=None):
        raw = self._run(
            query,
            parameters=parameters,
            settings=settings,
            output_format="JSONEachRow",
        )
        rows = tuple(json.loads(line) for line in raw.splitlines() if line.strip())

        class QueryResult:
            def named_results(self):
                return iter(rows)

        return QueryResult()

    def _run(
        self,
        query: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        data: bytes | None = None,
        output_format: str = "",
    ) -> str:
        command = [
            "docker",
            "exec",
            "-i",
            self.container,
            "clickhouse-client",
            "--database",
            self.database,
        ]
        for name, value in sorted((parameters or {}).items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise DashboardLoadError("invalid_clickhouse_parameter")
            command.append(f"--param_{name}={value}")
        for name, value in sorted((settings or {}).items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise DashboardLoadError("invalid_clickhouse_setting")
            command.append(f"--{name}={value}")
        command.extend(["--query", query])
        if output_format:
            command.extend(["--format", output_format])
        result = subprocess.run(command, input=data, capture_output=True)
        if result.returncode != 0:
            raise DashboardLoadError(
                f"clickhouse_ingestion_command_failed:{result.stderr.decode('utf-8', errors='replace').strip()}"
            )
        return result.stdout.decode("utf-8")


def load_market_dashboard_rows(
    overall_path: str | Path,
    channel_paths: Iterable[str | Path],
    *,
    snapshot_id: str,
    source_contract_path: str | Path = SOURCE_CONTRACT_PATH,
) -> tuple[MarketDashboardRows, SourceLoadManifest]:
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise DashboardLoadError("snapshot_id_required")
    contract = load_contract(source_contract_path)
    mapping = _field_mapping(contract)
    field_contracts = _field_contracts(contract, mapping)
    expected_headers = tuple(mapping.values())
    overall = Path(overall_path)
    if not overall.is_file():
        raise DashboardLoadError(f"source_file_missing:{overall}")
    overall_bounds = _filename_bounds(
        overall, pattern=OVERALL_FILENAME_PATTERN, kind="overall"
    )
    overall_rows = _read_source_file(
        overall,
        expected_headers=expected_headers,
        field_mapping=mapping,
        field_contracts=field_contracts,
        snapshot_id=snapshot_id,
        date_bounds=overall_bounds,
    )
    if not overall_rows:
        raise DashboardLoadError(f"overall_source_has_no_data:{overall}")

    channel_rows: list[dict[str, Any]] = []
    no_data_partitions: list[str] = []
    no_data_partition_windows: list[str] = []
    channel_window_ends: list[date] = []
    all_paths = [overall]
    seen_names = {overall.name}
    for raw_path in sorted(
        (Path(path) for path in channel_paths), key=lambda path: path.name
    ):
        if not raw_path.is_file():
            raise DashboardLoadError(f"source_file_missing:{raw_path}")
        if raw_path.name in seen_names:
            raise DashboardLoadError(f"duplicate_source_filename:{raw_path.name}")
        seen_names.add(raw_path.name)
        channel, start, end = _channel_filename_parts(raw_path)
        channel_window_ends.append(end)
        parsed = _read_source_file(
            raw_path,
            expected_headers=expected_headers,
            field_mapping=mapping,
            field_contracts=field_contracts,
            snapshot_id=snapshot_id,
            date_bounds=(start, end),
            channel=channel,
        )
        if parsed:
            channel_rows.extend(parsed)
        else:
            no_data_partitions.append(channel)
            no_data_partition_windows.append(
                f"{channel}@{start.isoformat()}:{end.isoformat()}"
            )
        all_paths.append(raw_path)

    overall_source_row_count = len(overall_rows)
    channel_source_row_count = len(channel_rows)
    overall_rows = _aggregate_to_contract_grain(
        overall_rows,
        ("snapshot_id", "business_date", "game"),
        field_contracts,
        OVERALL_DATASET_ID,
    )
    channel_rows = _aggregate_to_contract_grain(
        channel_rows,
        ("snapshot_id", "business_date", "game", "channel"),
        field_contracts,
        CHANNEL_DATASET_ID,
    )
    overall_schema = _table_schema_descriptor(field_contracts, channel=False)
    channel_schema = _table_schema_descriptor(field_contracts, channel=True)
    overall_fingerprint = schema_fingerprint(overall_schema)
    channel_fingerprint = schema_fingerprint(channel_schema)
    overall_table = _versioned_table_name(OVERALL_TABLE, overall_fingerprint)
    channel_table = _versioned_table_name(CHANNEL_TABLE, channel_fingerprint)
    checksums = {
        path.name: file_sha256(path)
        for path in sorted(all_paths, key=lambda path: path.name)
    }
    load_revision = content_ref(
        "dashboard-load",
        {
            "snapshot_id": snapshot_id,
            "source_checksums": checksums,
            "overall_rows_hash": rows_content_hash(overall_rows),
            "channel_rows_hash": rows_content_hash(channel_rows),
            "canonicalization_version": CANONICALIZATION_VERSION,
            "contract_ref": CONTRACT_REF,
            "overall_schema_fingerprint": overall_fingerprint,
            "channel_schema_fingerprint": channel_fingerprint,
        },
    )
    overall_rows = _attach_load_revision(overall_rows, load_revision)
    channel_rows = _attach_load_revision(channel_rows, load_revision)
    reconciliation = reconcile_paid_amount(overall_rows, channel_rows)
    overall_date_range = _date_range(overall_rows)
    channel_date_range = _date_range(channel_rows) if channel_rows else ()
    overall_fields = tuple(overall_rows[0])
    channel_fields = (
        tuple(channel_rows[0])
        if channel_rows
        else (*overall_fields[:4], "channel", *overall_fields[4:])
    )
    reconciliation_ref = content_ref("dashboard-reconciliation", asdict(reconciliation))

    manifest_values = {
        "snapshot_id": snapshot_id,
        "load_revision": load_revision,
        "dataset_id": OVERALL_DATASET_ID,
        "physical_table": overall_table,
        "channel_dataset_id": CHANNEL_DATASET_ID,
        "channel_physical_table": channel_table,
        "watermark": overall_date_range[1],
        "channel_watermark": (
            channel_date_range[1]
            if channel_date_range
            else max(channel_window_ends).isoformat()
            if channel_window_ends
            else overall_date_range[1]
        ),
        "overall_source_row_count": overall_source_row_count,
        "channel_source_row_count": channel_source_row_count,
        "row_count": len(overall_rows),
        "channel_row_count": len(channel_rows),
        "date_range": overall_date_range,
        "channel_date_range": channel_date_range,
        "schema_fields": overall_fields,
        "channel_schema_fields": channel_fields,
        "schema_fingerprint": overall_fingerprint,
        "channel_schema_fingerprint": channel_fingerprint,
        "overall_rows_content_hash": rows_content_hash(overall_rows),
        "channel_rows_content_hash": rows_content_hash(channel_rows),
        "source_checksums": checksums,
        "no_data_partitions": tuple(sorted(set(no_data_partitions))),
        "no_data_partition_windows": tuple(sorted(no_data_partition_windows)),
        "contract_ref": CONTRACT_REF,
        "runtime_binding_ref": RUNTIME_BINDING_REF,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "reconciliation_ref": reconciliation_ref,
    }
    manifest_ref = content_ref(
        "source-load-manifest",
        {**manifest_values, "reconciliation": asdict(reconciliation)},
    )
    overall_snapshot_ref = _dataset_snapshot_ref(
        manifest_ref=manifest_ref,
        snapshot_id=snapshot_id,
        dataset_id=OVERALL_DATASET_ID,
        physical_table=OVERALL_TABLE,
        watermark=overall_date_range[1],
        fingerprint=manifest_values["schema_fingerprint"],
        load_revision=load_revision,
    )
    channel_snapshot_ref = _dataset_snapshot_ref(
        manifest_ref=manifest_ref,
        snapshot_id=snapshot_id,
        dataset_id=CHANNEL_DATASET_ID,
        physical_table=channel_table,
        watermark=str(manifest_values["channel_watermark"]),
        fingerprint=channel_fingerprint,
        load_revision=load_revision,
    )
    manifest_values["release_ref"] = dataset_snapshot_release_ref(
        snapshot_id,
        load_revision,
        (overall_snapshot_ref, channel_snapshot_ref),
    )
    manifest = SourceLoadManifest(
        manifest_ref=manifest_ref,
        snapshot_ref=overall_snapshot_ref,
        **manifest_values,
        reconciliation=reconciliation,
    )
    return (
        MarketDashboardRows(
            snapshot_id=snapshot_id,
            load_revision=load_revision,
            overall_rows=tuple(overall_rows),
            channel_rows=tuple(channel_rows),
        ),
        manifest,
    )


def reconcile_paid_amount(
    overall_rows: Sequence[Mapping[str, Any]],
    channel_rows: Sequence[Mapping[str, Any]],
    *,
    tolerance: float = RECONCILIATION_TOLERANCE,
) -> ReconciliationResult:
    if not channel_rows:
        return ReconciliationResult(
            status="not_comparable",
            reasons=("channel_observations_absent",),
            compared_dates=(),
            observations=(),
            tolerance=tolerance,
        )
    overall = _paid_amount_totals(overall_rows)
    channel = _paid_amount_totals(channel_rows)
    observations: list[ReconciliationObservation] = []
    reasons: list[str] = []
    all_keys = sorted(set(overall) | set(channel))
    for business_date, game in all_keys:
        overall_amount = overall.get((business_date, game))
        channel_amount = channel.get((business_date, game))
        if overall_amount is None or channel_amount is None:
            status = "incomplete"
            difference = None
            reasons.append(f"paid_amount_observation_missing:{business_date}")
        else:
            difference = overall_amount - channel_amount
            status = (
                "matched" if abs(difference) <= Decimal(str(tolerance)) else "mismatch"
            )
            if status == "mismatch":
                reasons.append(f"paid_amount_mismatch:{business_date}")
        observations.append(
            ReconciliationObservation(
                business_date=business_date,
                game=game,
                overall_paid_amount=overall_amount,
                channel_paid_amount=channel_amount,
                difference=difference,
                status=status,
            )
        )
    unique_reasons = tuple(dict.fromkeys(reasons))
    statuses = {item.status for item in observations}
    overall_status = (
        "incomplete"
        if "incomplete" in statuses
        else "mismatch"
        if "mismatch" in statuses
        else "matched"
    )
    return ReconciliationResult(
        status=overall_status,
        reasons=unique_reasons,
        compared_dates=tuple(sorted({item.business_date for item in observations})),
        observations=tuple(observations),
        tolerance=tolerance,
    )


def stage_market_dashboard_release(
    client: Any,
    rows: MarketDashboardRows,
    manifest: SourceLoadManifest,
    *,
    active_load_revisions: Sequence[str],
) -> str:
    existing_overall = _read_persisted_rows(
        client, manifest.physical_table, rows.snapshot_id, manifest.load_revision
    )
    existing_channel = _read_persisted_rows(
        client,
        manifest.channel_physical_table,
        rows.snapshot_id,
        manifest.load_revision,
    )
    if existing_overall or existing_channel:
        try:
            validate_persisted_snapshot(client, rows, manifest)
            return "already_validated"
        except DashboardLoadError:
            if manifest.load_revision in set(active_load_revisions):
                raise DashboardLoadError("active_load_revision_invalid")
            for table in (manifest.physical_table, manifest.channel_physical_table):
                client.command(
                    "DELETE FROM "
                    + table
                    + " WHERE snapshot_id = {snapshot_id:String}"
                    + " AND load_revision = {load_revision:String}",
                    parameters={
                        "snapshot_id": rows.snapshot_id,
                        "load_revision": manifest.load_revision,
                    },
                    settings={"mutations_sync": 2},
                )
    insert_json_each_row(client, manifest.physical_table, rows.overall_rows)
    insert_json_each_row(client, manifest.channel_physical_table, rows.channel_rows)
    validate_persisted_snapshot(client, rows, manifest)
    return "staged_and_validated"


def validate_persisted_snapshot(
    client: Any,
    rows: MarketDashboardRows,
    manifest: SourceLoadManifest,
) -> PersistedSnapshotValidation:
    overall_persisted = _read_persisted_rows(
        client, manifest.physical_table, rows.snapshot_id, manifest.load_revision
    )
    channel_persisted = _read_persisted_rows(
        client,
        manifest.channel_physical_table,
        rows.snapshot_id,
        manifest.load_revision,
    )
    if len(overall_persisted) != manifest.row_count:
        raise DashboardLoadError("persisted_row_count_mismatch:market_dashboard")
    if len(channel_persisted) != manifest.channel_row_count:
        raise DashboardLoadError(
            "persisted_row_count_mismatch:market_dashboard_channel"
        )
    _validate_persisted_unique_keys(
        overall_persisted, OVERALL_ORDER_BY, OVERALL_DATASET_ID
    )
    _validate_persisted_unique_keys(
        channel_persisted, CHANNEL_ORDER_BY, CHANNEL_DATASET_ID
    )
    if rows_content_hash(overall_persisted) != manifest.overall_rows_content_hash:
        raise DashboardLoadError("persisted_rows_hash_mismatch:market_dashboard")
    if rows_content_hash(channel_persisted) != manifest.channel_rows_content_hash:
        raise DashboardLoadError(
            "persisted_rows_hash_mismatch:market_dashboard_channel"
        )
    overall_range = _date_range(overall_persisted)
    channel_range = _date_range(channel_persisted) if channel_persisted else ()
    if (
        overall_range != manifest.date_range
        or channel_range != manifest.channel_date_range
    ):
        raise DashboardLoadError("persisted_date_range_mismatch")
    reconciliation = reconcile_paid_amount(overall_persisted, channel_persisted)
    if asdict(reconciliation) != asdict(manifest.reconciliation):
        raise DashboardLoadError("persisted_reconciliation_mismatch")
    return PersistedSnapshotValidation(
        status="validated",
        overall_row_count=manifest.row_count,
        channel_row_count=manifest.channel_row_count,
        overall_date_range=overall_range,
        channel_date_range=channel_range,
        reconciliation=reconciliation,
    )


def build_dataset_snapshot_payloads(
    manifest: SourceLoadManifest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    loaded_at = _snapshot_available_at(manifest.watermark)
    common = {
        "watermark": manifest.watermark,
        "contract_ref": manifest.contract_ref,
        "loaded_at": loaded_at,
        "status": "active",
        "snapshot_id": manifest.snapshot_id,
        "logical_snapshot_id": manifest.snapshot_id,
        "load_revision": manifest.load_revision,
        "release_ref": manifest.release_ref,
        "requires_release": True,
        "evidence_state": "claim_ready",
        "reconciliation_status": manifest.reconciliation.status,
        "reconciliation_ref": manifest.reconciliation_ref,
        "source_load_manifest_ref": manifest.manifest_ref,
        "runtime_binding_ref": manifest.runtime_binding_ref,
        "source_checksums": dict(manifest.source_checksums),
        "no_data_partitions": list(manifest.no_data_partitions),
        "no_data_partition_windows": list(manifest.no_data_partition_windows),
        "reconciliation": json.loads(
            canonical_json_bytes(asdict(manifest.reconciliation))
        ),
    }
    overall = {
        **common,
        "snapshot_ref": manifest.snapshot_ref,
        "dataset_id": manifest.dataset_id,
        "physical_table": manifest.physical_table,
        "schema_fingerprint": manifest.schema_fingerprint,
        "schema_fields": list(manifest.schema_fields),
        "row_count": manifest.row_count,
        "date_range": list(manifest.date_range),
        "rows_content_hash": manifest.overall_rows_content_hash,
    }
    channel_ref = _dataset_snapshot_ref(
        manifest_ref=manifest.manifest_ref,
        snapshot_id=manifest.snapshot_id,
        dataset_id=manifest.channel_dataset_id,
        physical_table=manifest.channel_physical_table,
        watermark=manifest.channel_watermark,
        fingerprint=manifest.channel_schema_fingerprint,
        load_revision=manifest.load_revision,
    )
    channel = {
        **common,
        "snapshot_ref": channel_ref,
        "dataset_id": manifest.channel_dataset_id,
        "physical_table": manifest.channel_physical_table,
        "watermark": manifest.channel_watermark,
        "loaded_at": (
            _snapshot_available_at(manifest.channel_watermark)
            if manifest.channel_watermark
            else common["loaded_at"]
        ),
        "status": "active" if manifest.channel_row_count else "no_data",
        "evidence_state": (
            "claim_ready"
            if manifest.reconciliation.status == "matched"
            else "context_only"
        ),
        "schema_fingerprint": manifest.channel_schema_fingerprint,
        "schema_fields": list(manifest.channel_schema_fields),
        "row_count": manifest.channel_row_count,
        "date_range": list(manifest.channel_date_range),
        "rows_content_hash": manifest.channel_rows_content_hash,
    }
    return overall, channel


def persist_dataset_snapshot_payloads(
    store: Any,
    payloads: Sequence[Mapping[str, Any]],
) -> SnapshotPersistenceResult:
    try:
        normalized, logical_snapshot_id, _, release_ref = (
            validate_dataset_snapshot_release_payloads(payloads)
        )
    except ValueError as exc:
        raise DashboardLoadError(f"postgres_release_preflight:{exc}") from exc
    new_refs = {str(payload["snapshot_ref"]) for payload in normalized}
    existing = tuple(store.list_dataset_snapshots())
    superseded_refs = tuple(
        str(item["snapshot_ref"])
        for item in existing
        if item.get("logical_snapshot_id", item.get("snapshot_id"))
        == logical_snapshot_id
        and item.get("status") == "active"
        and item.get("snapshot_ref") not in new_refs
    )
    store.publish_dataset_snapshot_release(
        release_ref=release_ref,
        logical_snapshot_id=logical_snapshot_id,
        payloads=normalized,
    )
    try:
        authority = store.resolve_dataset_release(release_ref)
    except (KeyError, TypeError, ValueError) as exc:
        raise DashboardLoadError("postgres_release_authority_unavailable") from exc
    if dataset_release_authority_integrity_errors(authority):
        raise DashboardLoadError("postgres_release_authority_invalid")
    persisted = tuple(store.list_dataset_snapshots())
    by_ref = {str(item.get("snapshot_ref") or ""): dict(item) for item in persisted}
    for payload in normalized:
        current = {
            item["snapshot_ref"]
            for item in persisted
            if item.get("dataset_id") == payload["dataset_id"]
            if item.get("status") == "active"
            and item.get("logical_snapshot_id", item.get("snapshot_id"))
            == logical_snapshot_id
        }
        expected = {payload["snapshot_ref"]} if payload["status"] == "active" else set()
        if current != expected:
            raise DashboardLoadError(
                f"postgres_snapshot_roundtrip_failed:{payload['dataset_id']}"
            )
        if (
            payload["status"] == "active"
            and by_ref.get(str(payload["snapshot_ref"]), {}).get("authority_record_ref")
            != authority.authority_record_ref
        ):
            raise DashboardLoadError(
                f"postgres_snapshot_release_unverified:{payload['dataset_id']}"
            )
    return SnapshotPersistenceResult(
        active_refs=tuple(str(payload["snapshot_ref"]) for payload in normalized),
        superseded_refs=superseded_refs,
        verified_payloads=tuple(
            by_ref[str(payload["snapshot_ref"])] for payload in normalized
        ),
        authority_record=authority.to_dict(),
    )


def apply_clickhouse_ddl(
    client: Any,
    ddl_path: str | Path = DDL_PATH,
    *,
    overall_table: str = "",
    channel_table: str = "",
) -> None:
    if not overall_table or not channel_table:
        overall_table, channel_table = _current_physical_tables()
    existing = tuple(
        str(client.command(f"EXISTS TABLE {table}")).strip().lower() in {"1", "true"}
        for table in (overall_table, channel_table)
    )
    if not all(existing):
        ddl = Path(ddl_path).read_text(encoding="utf-8")
        ddl = ddl.split("-- BEGIN MARKET_DASHBOARD", 1)[1].split(
            "-- END MARKET_DASHBOARD", 1
        )[0]
        ddl = ddl.replace("__OVERALL_TABLE__", overall_table).replace(
            "__CHANNEL_TABLE__", channel_table
        )
        statements = [statement.strip() for statement in ddl.split(";")]
        for statement in statements:
            if statement:
                client.command(statement)
    validate_clickhouse_schema(
        client,
        overall_table=overall_table,
        channel_table=channel_table,
    )


def validate_clickhouse_schema(
    client: Any,
    *,
    overall_table: str = "",
    channel_table: str = "",
) -> None:
    if not overall_table or not channel_table:
        overall_table, channel_table = _current_physical_tables()
    for table in (overall_table, channel_table):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise DashboardLoadError("invalid_clickhouse_table")
    contract = load_contract(SOURCE_CONTRACT_PATH)
    specs = _field_contracts(contract, _field_mapping(contract))
    expected = {
        overall_table: _expected_clickhouse_columns(specs, channel=False),
        channel_table: _expected_clickhouse_columns(specs, channel=True),
    }
    column_result = client.query(
        f"""
        SELECT table, name, type, position
        FROM system.columns
        WHERE database = currentDatabase()
          AND table IN ('{overall_table}', '{channel_table}')
        ORDER BY table, position
        """
    )
    rows = tuple(dict(row) for row in column_result.named_results())
    observed = {
        table: tuple(
            (str(row["name"]), str(row["type"]))
            for row in rows
            if row.get("table") == table
        )
        for table in expected
    }
    if observed != expected:
        raise DashboardLoadError("clickhouse_schema_drift:columns")
    table_result = client.query(
        f"""
        SELECT name, engine, sorting_key
        FROM system.tables
        WHERE database = currentDatabase()
          AND name IN ('{overall_table}', '{channel_table}')
        ORDER BY name
        """
    )
    table_rows = {str(row["name"]): dict(row) for row in table_result.named_results()}
    for table, order_by in (
        (overall_table, OVERALL_ORDER_BY),
        (channel_table, CHANNEL_ORDER_BY),
    ):
        row = table_rows.get(table, {})
        normalized_sorting = re.sub(r"[()`\s]", "", str(row.get("sorting_key") or ""))
        if row.get("engine") != TABLE_ENGINE or normalized_sorting != ",".join(
            order_by
        ):
            raise DashboardLoadError(f"clickhouse_schema_drift:table:{table}")


def _expected_clickhouse_columns(
    specs: Mapping[str, Mapping[str, Any]],
    *,
    channel: bool,
) -> tuple[tuple[str, str], ...]:
    columns = [
        ("snapshot_id", "String"),
        ("load_revision", "String"),
        ("business_date", "Date"),
        ("game", "String"),
    ]
    if channel:
        columns.append(("channel", "String"))
    for field, spec in specs.items():
        if field in {"business_date", "game"}:
            continue
        data_type = str(spec["clickhouse_type"])
        if spec["nullable"]:
            data_type = f"Nullable({data_type})"
        columns.append((field, data_type))
    return tuple(columns)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overall", type=Path, required=True)
    parser.add_argument("--channels", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--manifest-out", type=Path, required=True)
    parser.add_argument("--clickhouse-container", default="")
    parser.add_argument("--rebuild-schema", action="store_true")
    parser.add_argument("--skip-postgres", action="store_true")
    args = parser.parse_args(argv)
    if not args.channels.is_dir():
        raise DashboardLoadError(f"source_directory_missing:{args.channels}")

    rows, manifest = load_market_dashboard_rows(
        args.overall,
        tuple(sorted(args.channels.glob("*.csv"))),
        snapshot_id=args.snapshot_id,
    )
    if args.clickhouse_container:
        database = os.environ.get("WAJE_CLICKHOUSE_DATABASE", "")
        if not database:
            raise DashboardLoadError(
                "missing_clickhouse_binding:WAJE_CLICKHOUSE_DATABASE"
            )
        client = DockerClickHouseClient(args.clickhouse_container, database)
    else:
        runtime = ClickHouseRuntime.from_env()
        if not runtime.configured():
            raise DashboardLoadError(
                "missing_clickhouse_binding:" + ",".join(runtime.binding.missing)
            )
        client = runtime._get_client()
    payloads = build_dataset_snapshot_payloads(manifest)

    postgres_result = SnapshotPersistenceResult(active_refs=(), superseded_refs=())
    store = None if args.skip_postgres else PostgresConversationStore.from_env()
    try:
        if store is None:
            _prepare_clickhouse_schema(
                client,
                manifest=manifest,
                rebuild=args.rebuild_schema,
                container_mode=bool(args.clickhouse_container),
            )
            stage_market_dashboard_release(
                client,
                rows,
                manifest,
                active_load_revisions=(),
            )
        else:
            with store.dataset_snapshot_release_lock(manifest.snapshot_id):
                _prepare_clickhouse_schema(
                    client,
                    manifest=manifest,
                    rebuild=args.rebuild_schema,
                    container_mode=bool(args.clickhouse_container),
                )
                active_revisions = tuple(
                    str(item.get("load_revision") or "")
                    for item in store.list_dataset_snapshots()
                    if item.get("logical_snapshot_id", item.get("snapshot_id"))
                    == manifest.snapshot_id
                    and item.get("status") == "active"
                )
                stage_market_dashboard_release(
                    client,
                    rows,
                    manifest,
                    active_load_revisions=active_revisions,
                )
                postgres_result = persist_dataset_snapshot_payloads(store, payloads)
        validation = validate_persisted_snapshot(client, rows, manifest)
    finally:
        if store is not None:
            close = getattr(store.connection, "close", None)
            if callable(close):
                close()

    artifact = {
        "source_load_manifest": manifest.to_dict(),
        "dataset_snapshot_payloads": list(
            postgres_result.verified_payloads or payloads
        ),
        "persisted_validation": asdict(validation),
        "postgres_snapshot_roundtrip_refs": list(postgres_result.active_refs),
        "postgres_superseded_snapshot_refs": list(postgres_result.superseded_refs),
        "dataset_release_authority": postgres_result.authority_record,
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
                "overall_row_count": manifest.row_count,
                "channel_row_count": manifest.channel_row_count,
                "watermark": manifest.watermark,
                "reconciliation_status": manifest.reconciliation.status,
                "reconciliation_reason_count": len(manifest.reconciliation.reasons),
                "postgres_snapshot_refs": postgres_result.active_refs,
                "postgres_superseded_snapshot_count": len(
                    postgres_result.superseded_refs
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _prepare_clickhouse_schema(
    client: Any,
    *,
    manifest: SourceLoadManifest | None = None,
    rebuild: bool,
    container_mode: bool,
) -> None:
    if rebuild and not container_mode:
        raise DashboardLoadError("schema_rebuild_requires_ingestion_control_plane")
    overall_table, channel_table = (
        (manifest.physical_table, manifest.channel_physical_table)
        if manifest is not None
        else _current_physical_tables()
    )
    apply_clickhouse_ddl(
        client,
        overall_table=overall_table,
        channel_table=channel_table,
    )


def _field_mapping(contract: Mapping[str, Any]) -> dict[str, str]:
    raw = contract.get("field_mapping")
    if not isinstance(raw, Mapping) or not raw:
        raise DashboardLoadError("source_contract_field_mapping_missing")
    mapping = {str(field): str(source) for field, source in raw.items()}
    if set(mapping) < {"business_date", "game", "paid_amount"}:
        raise DashboardLoadError("source_contract_required_fields_missing")
    if len(set(mapping.values())) != len(mapping):
        raise DashboardLoadError("source_contract_duplicate_headers")
    return mapping


def _field_contracts(
    contract: Mapping[str, Any],
    mapping: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    raw = contract.get("field_contracts")
    if not isinstance(raw, Mapping) or set(raw) != set(mapping):
        raise DashboardLoadError("source_contract_field_contracts_incomplete")
    required = {
        "source_field",
        "logical_type",
        "clickhouse_type",
        "precision",
        "scale",
        "nullable",
        "missing_tokens",
        "duplicate_aggregation",
        "value_semantics",
        "display_format",
        "rounding_mode",
        "loss_policy",
        "canonicalization_version",
    }
    result = {}
    for field, value in raw.items():
        if not isinstance(value, Mapping) or not required.issubset(value):
            raise DashboardLoadError(f"source_field_contract_incomplete:{field}")
        spec = dict(value)
        if spec["source_field"] != mapping[field]:
            raise DashboardLoadError(f"source_field_contract_mapping_mismatch:{field}")
        if spec["duplicate_aggregation"] not in {
            "key",
            "additive_sum",
            "single_nonzero_signal",
        }:
            raise DashboardLoadError(
                f"source_field_contract_aggregation_invalid:{field}"
            )
        if spec["canonicalization_version"] != CANONICALIZATION_VERSION:
            raise DashboardLoadError(
                f"source_field_contract_canonicalization_invalid:{field}"
            )
        if spec["rounding_mode"] not in {"not_applicable", "half_even"}:
            raise DashboardLoadError(f"source_field_contract_rounding_invalid:{field}")
        if spec["loss_policy"] not in {
            "reject_invalid",
            "round_to_declared_scale",
        }:
            raise DashboardLoadError(
                f"source_field_contract_loss_policy_invalid:{field}"
            )
        result[str(field)] = spec
    return result


def _read_source_file(
    path: Path,
    *,
    expected_headers: Sequence[str],
    field_mapping: Mapping[str, str],
    field_contracts: Mapping[str, Mapping[str, Any]],
    snapshot_id: str,
    date_bounds: tuple[date, date],
    channel: str = "",
) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        actual_headers = tuple(reader.fieldnames or ())
        _validate_headers(path, actual_headers, expected_headers)
        rows: list[dict[str, Any]] = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise DashboardLoadError(
                    f"source_row_extra_cells:{path}:row={row_number}"
                )
            if any(value is None for value in raw.values()):
                raise DashboardLoadError(
                    f"source_row_missing_cells:{path}:row={row_number}"
                )
            business_date = _parse_date(
                raw[field_mapping["business_date"]], path, row_number
            )
            if not date_bounds[0] <= business_date <= date_bounds[1]:
                raise DashboardLoadError(
                    f"business_date_outside_filename_range:{path}:row={row_number}"
                )
            game = str(raw[field_mapping["game"]] or "").strip()
            if not game:
                raise DashboardLoadError(
                    f"grain_key_empty:game:{path}:row={row_number}"
                )
            allowed_games = tuple(field_contracts["game"].get("allowed_values") or ())
            if allowed_games and game not in allowed_games:
                raise DashboardLoadError(f"game_scope_mismatch:{path}:row={row_number}")
            parsed: dict[str, Any] = {
                "snapshot_id": snapshot_id,
                "business_date": business_date.isoformat(),
                "game": game,
            }
            if channel:
                parsed["channel"] = channel
            for field, spec in field_contracts.items():
                if field in {"business_date", "game"}:
                    continue
                source_field = str(spec["source_field"])
                parsed[field] = _parse_contract_number(
                    raw[source_field],
                    spec=spec,
                    path=path,
                    row_number=row_number,
                    field=source_field,
                )
            rows.append(parsed)
        return rows


def _validate_headers(
    path: Path, actual: Sequence[str], expected: Sequence[str]
) -> None:
    if tuple(actual) == tuple(expected):
        return
    missing = tuple(field for field in expected if field not in actual)
    unexpected = tuple(field for field in actual if field not in expected)
    order_mismatch = not missing and not unexpected
    raise DashboardLoadError(
        f"source_header_mismatch:{path}:missing={','.join(missing)}:"
        f"unexpected={','.join(unexpected)}:order_mismatch={str(order_mismatch).lower()}"
    )


def _parse_contract_number(
    value: Any,
    *,
    spec: Mapping[str, Any],
    path: Path,
    row_number: int,
    field: str,
) -> Decimal | None:
    normalized = str(value or "").strip()
    missing_tokens = {str(item).casefold() for item in spec.get("missing_tokens") or ()}
    if normalized.casefold() in missing_tokens:
        if not bool(spec.get("nullable")):
            raise DashboardLoadError(
                f"non_nullable_value_missing:{path}:row={row_number}:field={field}"
            )
        return None
    try:
        parsed = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise DashboardLoadError(
            f"invalid_numeric_value:{path}:row={row_number}:field={field}"
        ) from exc
    if not parsed.is_finite():
        raise DashboardLoadError(
            f"invalid_numeric_value:{path}:row={row_number}:field={field}"
        )
    scale = spec.get("scale")
    precision = spec.get("precision")
    if not isinstance(scale, int) or not isinstance(precision, int):
        raise DashboardLoadError(f"source_field_contract_decimal_invalid:{field}")
    with localcontext() as context:
        context.prec = max(precision + scale + 4, 80)
        try:
            if spec.get("rounding_mode") != "half_even":
                raise DashboardLoadError(
                    f"source_field_contract_rounding_invalid:{field}"
                )
            quantized = parsed.quantize(
                Decimal(1).scaleb(-scale),
                rounding=ROUND_HALF_EVEN,
            )
        except InvalidOperation as exc:
            raise DashboardLoadError(
                f"numeric_precision_overflow:{path}:row={row_number}:field={field}"
            ) from exc
    integer_digits = max(quantized.adjusted() + 1, 0)
    if integer_digits + scale > precision:
        raise DashboardLoadError(
            f"numeric_precision_overflow:{path}:row={row_number}:field={field}"
        )
    return quantized


def _parse_date(value: Any, path: Path, row_number: int) -> date:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise DashboardLoadError(
            f"invalid_business_date:{path}:row={row_number}"
        ) from exc


def _filename_bounds(
    path: Path, *, pattern: re.Pattern[str], kind: str
) -> tuple[date, date]:
    match = pattern.fullmatch(path.name)
    if not match:
        raise DashboardLoadError(f"{kind}_filename_contract_mismatch:{path.name}")
    start, end = date.fromisoformat(match["start"]), date.fromisoformat(match["end"])
    if start > end:
        raise DashboardLoadError(f"filename_date_range_invalid:{path.name}")
    return start, end


def _channel_filename_parts(path: Path) -> tuple[str, date, date]:
    match = CHANNEL_FILENAME_PATTERN.fullmatch(path.name)
    if not match or not match["channel"].strip():
        raise DashboardLoadError(f"channel_filename_contract_mismatch:{path.name}")
    start, end = date.fromisoformat(match["start"]), date.fromisoformat(match["end"])
    if start > end:
        raise DashboardLoadError(f"filename_date_range_invalid:{path.name}")
    return match["channel"], start, end


def _aggregate_to_contract_grain(
    rows: Sequence[Mapping[str, Any]],
    unique_key: Sequence[str],
    field_contracts: Mapping[str, Mapping[str, Any]],
    dataset_id: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = tuple(row[field] for field in unique_key)
        grouped.setdefault(key, []).append(row)
    aggregated: list[dict[str, Any]] = []
    for key in sorted(grouped):
        group = grouped[key]
        if len(group) == 1:
            aggregated.append(dict(group[0]))
            continue
        result = {field: value for field, value in zip(unique_key, key)}
        for field, spec in field_contracts.items():
            if field in {"business_date", "game"}:
                continue
            values = tuple(row[field] for row in group)
            policy = spec["duplicate_aggregation"]
            if policy == "additive_sum":
                present = tuple(value for value in values if value is not None)
                result[field] = sum(present) if present else None
                continue
            if policy != "single_nonzero_signal":
                raise DashboardLoadError(
                    f"duplicate_aggregation_policy_invalid:{dataset_id}:{field}"
                )
            signals = {value for value in values if value not in (None, Decimal(0))}
            if len(signals) > 1:
                raise DashboardLoadError(
                    f"duplicate_non_additive_conflict:{dataset_id}:{field}:{key}"
                )
            if signals:
                result[field] = next(iter(signals))
            elif any(value is not None for value in values):
                result[field] = Decimal(0).quantize(
                    Decimal(1).scaleb(-int(spec["scale"]))
                )
            else:
                result[field] = None
        aggregated.append(result)
    return aggregated


def _attach_load_revision(
    rows: Sequence[Mapping[str, Any]],
    load_revision: str,
) -> list[dict[str, Any]]:
    attached = []
    for row in rows:
        attached.append(
            {
                "snapshot_id": row["snapshot_id"],
                "load_revision": load_revision,
                **{key: value for key, value in row.items() if key != "snapshot_id"},
            }
        )
    return attached


def _table_schema_descriptor(
    field_contracts: Mapping[str, Mapping[str, Any]],
    *,
    channel: bool,
) -> tuple[str, ...]:
    fields = ["snapshot_id:String:not_null", "load_revision:String:not_null"]
    fields.append("business_date:Date:not_null")
    fields.append("game:String:not_null")
    if channel:
        fields.append("channel:String:not_null")
    for field, spec in field_contracts.items():
        if field in {"business_date", "game"}:
            continue
        clickhouse_type = str(spec["clickhouse_type"])
        if spec["nullable"]:
            clickhouse_type = f"Nullable({clickhouse_type})"
        fields.append(
            f"{field}:{clickhouse_type}:{'nullable' if spec['nullable'] else 'not_null'}"
        )
        fields.append(
            "policy:"
            f"{field}:{spec['rounding_mode']}:{spec['loss_policy']}:"
            f"{spec['canonicalization_version']}"
        )
    order_by = CHANNEL_ORDER_BY if channel else OVERALL_ORDER_BY
    return (
        *fields,
        f"engine:{TABLE_ENGINE}",
        "order_by:" + ",".join(order_by),
        f"canonicalization:{CANONICALIZATION_VERSION}",
    )


def _versioned_table_name(base: str, fingerprint: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", base):
        raise DashboardLoadError("invalid_clickhouse_table_prefix")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DashboardLoadError("invalid_schema_fingerprint")
    return f"{base}__{fingerprint[:16]}"


def _current_physical_tables() -> tuple[str, str]:
    contract = load_contract(SOURCE_CONTRACT_PATH)
    specs = _field_contracts(contract, _field_mapping(contract))
    overall_fingerprint = schema_fingerprint(
        _table_schema_descriptor(specs, channel=False)
    )
    channel_fingerprint = schema_fingerprint(
        _table_schema_descriptor(specs, channel=True)
    )
    return (
        _versioned_table_name(OVERALL_TABLE, overall_fingerprint),
        _versioned_table_name(CHANNEL_TABLE, channel_fingerprint),
    )


def _is_channel_table(table: str) -> bool:
    return table == CHANNEL_TABLE or table.startswith(f"{CHANNEL_TABLE}__")


def _date_range(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    values = tuple(str(row["business_date"]) for row in rows)
    return min(values), max(values)


def _paid_amount_totals(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Decimal | None]:
    totals: dict[tuple[str, str], Decimal | None] = {}
    for row in rows:
        key = (str(row["business_date"]), str(row.get("game") or ""))
        value = row.get("paid_amount")
        if value is None or totals.get(key) is None and key in totals:
            totals[key] = None
        else:
            totals[key] = Decimal(totals.get(key) or 0) + Decimal(value)
    return totals


def _dataset_snapshot_ref(
    *,
    manifest_ref: str,
    snapshot_id: str,
    dataset_id: str,
    physical_table: str,
    watermark: str,
    fingerprint: str,
    load_revision: str,
) -> str:
    return content_ref(
        "dataset-snapshot",
        {
            "manifest_ref": manifest_ref,
            "snapshot_id": snapshot_id,
            "dataset_id": dataset_id,
            "physical_table": physical_table,
            "watermark": watermark,
            "schema_fingerprint": fingerprint,
            "load_revision": load_revision,
        },
    )


def _snapshot_available_at(watermark: str) -> str:
    available = datetime.combine(
        date.fromisoformat(watermark) + timedelta(days=1),
        time.min,
        tzinfo=timezone.utc,
    )
    return available.isoformat()


def _read_persisted_rows(
    client: Any,
    table: str,
    snapshot_id: str,
    load_revision: str,
) -> tuple[dict[str, Any], ...]:
    result = client.query(
        f"""
        SELECT *
        FROM {table}
        WHERE snapshot_id = {{snapshot_id:String}}
          AND load_revision = {{load_revision:String}}
        ORDER BY snapshot_id, load_revision, business_date, game{", channel" if _is_channel_table(table) else ""}
        """,
        parameters={"snapshot_id": snapshot_id, "load_revision": load_revision},
    )
    if hasattr(result, "named_results"):
        raw_rows = tuple(dict(row) for row in result.named_results())
    else:
        columns = tuple(getattr(result, "column_names", ()) or ())
        raw_rows = tuple(
            dict(zip(columns, row)) for row in getattr(result, "result_rows", ()) or ()
        )
    contract = load_contract(SOURCE_CONTRACT_PATH)
    specs = _field_contracts(contract, _field_mapping(contract))
    expected_fields = (
        "snapshot_id",
        "load_revision",
        "business_date",
        "game",
        *(("channel",) if _is_channel_table(table) else ()),
        *(field for field in specs if field not in {"business_date", "game"}),
    )
    normalized_rows = []
    for row in raw_rows:
        if set(row) != set(expected_fields):
            raise DashboardLoadError(f"persisted_schema_fields_mismatch:{table}")
        normalized = {
            "snapshot_id": str(row["snapshot_id"]),
            "load_revision": str(row["load_revision"]),
            "business_date": str(row["business_date"]),
            "game": str(row["game"]),
        }
        if _is_channel_table(table):
            normalized["channel"] = str(row["channel"])
        for field, spec in specs.items():
            if field in {"business_date", "game"}:
                continue
            value = row[field]
            normalized[field] = (
                None
                if value is None
                else _quantize_persisted_decimal(value, spec=spec, field=field)
            )
        normalized_rows.append(normalized)
    return tuple(normalized_rows)


def _quantize_persisted_decimal(
    value: Any,
    *,
    spec: Mapping[str, Any],
    field: str,
) -> Decimal:
    try:
        number = Decimal(str(value))
        with localcontext() as context:
            context.prec = max(int(spec["precision"]) + int(spec["scale"]) + 4, 80)
            return number.quantize(
                Decimal(1).scaleb(-int(spec["scale"])),
                rounding=ROUND_HALF_EVEN,
            )
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DashboardLoadError(f"persisted_decimal_invalid:{field}") from exc


def _validate_persisted_unique_keys(
    rows: Sequence[Mapping[str, Any]],
    unique_key: Sequence[str],
    dataset_id: str,
) -> None:
    keys = [tuple(row[field] for field in unique_key) for row in rows]
    if len(keys) != len(set(keys)):
        raise DashboardLoadError(f"persisted_unique_key_invalid:{dataset_id}")


if __name__ == "__main__":
    raise SystemExit(main())
