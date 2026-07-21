from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
import zipfile


BACKUP_FORMAT = "waje-runtime-csv-zip.v1"
RUNTIME_SCHEMA = "waje_runtime"
REQUIRED_CUTOVER_TABLES = frozenset({"run_dispatches", "schema_migrations"})
TABLE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
SNAPSHOT_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]+")
CONTAINER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class RuntimeBackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeBackupArtifact:
    path: Path
    sha256: str
    table_count: int
    row_count: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "backup_format": BACKUP_FORMAT,
            "created_at": self.created_at,
            "path": str(self.path),
            "row_count": self.row_count,
            "sha256": self.sha256,
            "table_count": self.table_count,
        }


SchemaDumper = Callable[[str], bytes]


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _sha256_path(path: Path) -> str:
    with path.open("rb") as source:
        return _sha256_stream(source)[0]


def _sha256_stream(source: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _created_at(value: datetime | None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise RuntimeBackupError("backup_created_at_timezone_required")
    return (
        timestamp.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _absolute_output_path(output_path: str | Path) -> Path:
    path = Path(output_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.suffix != ".zip":
        raise RuntimeBackupError("backup_output_zip_required")
    if path.exists():
        raise RuntimeBackupError("backup_output_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _staged_path(target: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(raw_path)


def _publish_without_overwrite(staged: Path, target: Path) -> None:
    try:
        os.link(staged, target)
    except FileExistsError as exc:
        raise RuntimeBackupError("backup_output_exists") from exc


def _table_names(connection: Any) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (RUNTIME_SCHEMA,),
    ).fetchall()
    tables = tuple(str(row[0]) for row in rows)
    if not tables or any(TABLE_IDENTIFIER.fullmatch(table) is None for table in tables):
        raise RuntimeBackupError("runtime_backup_table_scope_invalid")
    if len(tables) != len(set(tables)):
        raise RuntimeBackupError("runtime_backup_table_scope_invalid")
    missing = REQUIRED_CUTOVER_TABLES - set(tables)
    if missing:
        raise RuntimeBackupError(
            "runtime_backup_required_tables_missing:" + ",".join(sorted(missing))
        )
    return tables


def _lock_tables(connection: Any, tables: tuple[str, ...]) -> None:
    qualified = ", ".join(
        f"{_quoted_identifier(RUNTIME_SCHEMA)}.{_quoted_identifier(table)}"
        for table in tables
    )
    connection.execute(f"LOCK TABLE {qualified} IN ACCESS SHARE MODE")


def _export_snapshot(connection: Any) -> str:
    row = connection.execute("SELECT pg_export_snapshot()").fetchone()
    snapshot_id = str(row[0]) if row and row[0] is not None else ""
    if SNAPSHOT_IDENTIFIER.fullmatch(snapshot_id) is None:
        raise RuntimeBackupError("runtime_backup_snapshot_invalid")
    return snapshot_id


def _live_schema_metadata(connection: Any) -> dict[str, list[dict[str, object]]]:
    columns = [
        {
            "column": str(column),
            "data_type": str(data_type),
            "default": default,
            "nullable": str(nullable),
            "ordinal": int(ordinal),
            "table": str(table),
            "udt_name": str(udt_name),
        }
        for table, column, ordinal, nullable, data_type, udt_name, default in connection.execute(
            """
            SELECT
              table_name,
              column_name,
              ordinal_position,
              is_nullable,
              data_type,
              udt_name,
              column_default
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            (RUNTIME_SCHEMA,),
        ).fetchall()
    ]
    constraints = [
        {
            "definition": str(definition),
            "name": str(name),
            "table": str(table),
            "type": str(constraint_type),
        }
        for table, name, constraint_type, definition in connection.execute(
            """
            SELECT
              relation.relname,
              constraint_record.conname,
              constraint_record.contype,
              pg_get_constraintdef(constraint_record.oid, true)
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation
              ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace_record
              ON namespace_record.oid = relation.relnamespace
            WHERE namespace_record.nspname = %s
            ORDER BY relation.relname, constraint_record.conname
            """,
            (RUNTIME_SCHEMA,),
        ).fetchall()
    ]
    indexes = [
        {
            "definition": str(definition),
            "name": str(name),
            "table": str(table),
        }
        for table, name, definition in connection.execute(
            """
            SELECT tablename, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = %s
            ORDER BY tablename, indexname
            """,
            (RUNTIME_SCHEMA,),
        ).fetchall()
    ]
    migration_ledger = [
        {
            "migration_digest": str(migration_digest),
            "migration_id": str(migration_id),
        }
        for migration_id, migration_digest in connection.execute(
            """
            SELECT migration_id, migration_digest
            FROM waje_runtime.schema_migrations
            ORDER BY migration_id
            """
        ).fetchall()
    ]
    triggers = [
        {
            "definition": str(definition),
            "name": str(name),
            "table": str(table),
        }
        for table, name, definition in connection.execute(
            """
            SELECT
              relation.relname,
              trigger_record.tgname,
              pg_get_triggerdef(trigger_record.oid, true)
            FROM pg_trigger AS trigger_record
            JOIN pg_class AS relation
              ON relation.oid = trigger_record.tgrelid
            JOIN pg_namespace AS namespace_record
              ON namespace_record.oid = relation.relnamespace
            WHERE namespace_record.nspname = %s
              AND NOT trigger_record.tgisinternal
            ORDER BY relation.relname, trigger_record.tgname
            """,
            (RUNTIME_SCHEMA,),
        ).fetchall()
    ]
    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "migration_ledger": migration_ledger,
        "triggers": triggers,
    }


def _copy_table(
    connection: Any,
    archive: zipfile.ZipFile,
    table: str,
) -> dict[str, object]:
    member = f"tables/{table}.csv"
    digest = hashlib.sha256()
    uncompressed_bytes = 0
    copy_statement = (
        f"COPY {_quoted_identifier(RUNTIME_SCHEMA)}.{_quoted_identifier(table)} "
        "TO STDOUT WITH (FORMAT CSV, HEADER TRUE, ENCODING 'UTF8')"
    )
    with connection.cursor() as cursor:
        with archive.open(member, "w", force_zip64=True) as destination:
            with cursor.copy(copy_statement) as copy:
                for chunk in copy:
                    payload = bytes(chunk)
                    destination.write(payload)
                    digest.update(payload)
                    uncompressed_bytes += len(payload)
        row_count = int(cursor.rowcount)
    if row_count < 0:
        raise RuntimeBackupError("runtime_backup_copy_row_count_unavailable")
    return {
        "member": member,
        "row_count": row_count,
        "sha256": digest.hexdigest(),
        "table": table,
        "uncompressed_bytes": uncompressed_bytes,
    }


def _write_staged_archive(
    connection: Any,
    staged: Path,
    *,
    schema_dumper: SchemaDumper,
    created_at: str,
) -> tuple[list[dict[str, object]], str]:
    connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    tables = _table_names(connection)
    _lock_tables(connection, tables)
    snapshot_id = _export_snapshot(connection)
    schema_sql = schema_dumper(snapshot_id)
    if not schema_sql.strip():
        raise RuntimeBackupError("runtime_backup_schema_dump_empty")
    metadata = _live_schema_metadata(connection)
    schema_digest = hashlib.sha256(schema_sql).hexdigest()
    records: list[dict[str, object]] = []
    with zipfile.ZipFile(
        staged,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        archive.writestr("schema/conversation-runtime.sql", schema_sql)
        archive.writestr(
            "schema/live-schema-metadata.json",
            _json_bytes(metadata),
        )
        for table in tables:
            records.append(_copy_table(connection, archive, table))
        archive.writestr(
            "manifest.json",
            _json_bytes(
                {
                    "backup_format": BACKUP_FORMAT,
                    "created_at": created_at,
                    "schema": RUNTIME_SCHEMA,
                    "schema_contract_sha256": schema_digest,
                    "tables": records,
                }
            ),
        )
    return records, schema_digest


def _verify_staged_archive(
    staged: Path,
    records: list[dict[str, object]],
    schema_digest: str,
) -> None:
    with zipfile.ZipFile(staged) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise RuntimeBackupError("runtime_backup_archive_verification_failed")
        manifest = json.loads(archive.read("manifest.json"))
        if (
            manifest.get("backup_format") != BACKUP_FORMAT
            or manifest.get("schema") != RUNTIME_SCHEMA
            or manifest.get("schema_contract_sha256") != schema_digest
            or manifest.get("tables") != records
            or hashlib.sha256(
                archive.read("schema/conversation-runtime.sql")
            ).hexdigest()
            != schema_digest
            or "schema/live-schema-metadata.json" not in names
        ):
            raise RuntimeBackupError("runtime_backup_archive_verification_failed")
        for record in records:
            with archive.open(str(record["member"])) as member:
                member_digest, member_size = _sha256_stream(member)
            if (
                member_size != record["uncompressed_bytes"]
                or member_digest != record["sha256"]
            ):
                raise RuntimeBackupError("runtime_backup_archive_verification_failed")


def create_runtime_backup(
    connection: Any,
    output_path: str | Path,
    *,
    schema_dumper: SchemaDumper,
    timestamp: datetime | None = None,
) -> RuntimeBackupArtifact:
    target = _absolute_output_path(output_path)
    staged = _staged_path(target)
    created_at = _created_at(timestamp)
    records: list[dict[str, object]] = []
    try:
        try:
            records, schema_digest = _write_staged_archive(
                connection,
                staged,
                schema_dumper=schema_dumper,
                created_at=created_at,
            )
            _verify_staged_archive(staged, records, schema_digest)
        finally:
            connection.rollback()
        with staged.open("rb") as source:
            os.fsync(source.fileno())
        archive_digest = _sha256_path(staged)
        _publish_without_overwrite(staged, target)
        return RuntimeBackupArtifact(
            path=target,
            sha256=archive_digest,
            table_count=len(records),
            row_count=sum(int(record["row_count"]) for record in records),
            created_at=created_at,
        )
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        if isinstance(exc, RuntimeBackupError):
            raise
        raise RuntimeBackupError("runtime_backup_artifact_failed") from exc
    finally:
        staged.unlink(missing_ok=True)


def _container_pg_environment(database_url: str) -> dict[str, str]:
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError as exc:
        raise RuntimeBackupError("psycopg_required") from exc
    values = conninfo_to_dict(database_url)
    mapping = {
        "dbname": "PGDATABASE",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "sslmode": "PGSSLMODE",
    }
    return {
        environment_name: str(values[parameter])
        for parameter, environment_name in mapping.items()
        if values.get(parameter) is not None
    }


def _pg_dump_environment(database_url: str, *, container: bool) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR"}
    }
    if container:
        environment.update(_container_pg_environment(database_url))
    else:
        environment["PGDATABASE"] = database_url
    return environment


def pg_dump_schema_dumper(
    database_url: str,
    *,
    pg_dump_binary: str = "pg_dump",
    container: str | None = None,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> SchemaDumper:
    if not pg_dump_binary.strip():
        raise RuntimeBackupError("pg_dump_binary_required")
    if container is not None and CONTAINER_IDENTIFIER.fullmatch(container) is None:
        raise RuntimeBackupError("pg_dump_container_invalid")
    environment = _pg_dump_environment(
        database_url,
        container=container is not None,
    )

    def dump(snapshot_id: str) -> bytes:
        if SNAPSHOT_IDENTIFIER.fullmatch(snapshot_id) is None:
            raise RuntimeBackupError("runtime_backup_snapshot_invalid")
        pg_dump_arguments = [
            pg_dump_binary,
            "--schema-only",
            "--format=plain",
            f"--schema={RUNTIME_SCHEMA}",
            f"--snapshot={snapshot_id}",
            "--encoding=UTF8",
            "--no-owner",
            "--no-privileges",
            "--no-password",
        ]
        if container is None:
            command = pg_dump_arguments
        else:
            forwarded_environment = [
                argument
                for name in sorted(environment)
                if name.startswith("PG")
                for argument in ("--env", name)
            ]
            command = [
                "docker",
                "exec",
                *forwarded_environment,
                container,
                *pg_dump_arguments,
            ]
        try:
            result = subprocess_run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
            )
        except OSError as exc:
            raise RuntimeBackupError("pg_dump_unavailable") from exc
        if result.returncode != 0:
            raise RuntimeBackupError("pg_dump_failed")
        schema_sql = bytes(result.stdout)
        if not schema_sql.strip():
            raise RuntimeBackupError("pg_dump_empty")
        return schema_sql

    return dump


def _runtime_database_url() -> str:
    database_url = os.environ.get("WAJE_RUNTIME_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not database_url:
        raise RuntimeBackupError("runtime_database_url_required")
    return database_url


def _connect(database_url: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeBackupError("psycopg_required") from exc
    return psycopg.connect(database_url)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create an immutable, snapshot-consistent ZIP backup of the "
            "waje_runtime PostgreSQL schema."
        )
    )
    parser.add_argument("--output", required=True, help="New .zip artifact path.")
    parser.add_argument(
        "--pg-dump",
        default="pg_dump",
        help="pg_dump executable on the host or inside --pg-dump-container.",
    )
    parser.add_argument(
        "--pg-dump-container",
        help=(
            "Run pg_dump in this existing Docker container. The database, user, "
            "and password are forwarded through process environment variables."
        ),
    )
    args = parser.parse_args()
    database_url = _runtime_database_url()
    connection = _connect(database_url)
    try:
        artifact = create_runtime_backup(
            connection,
            args.output,
            schema_dumper=pg_dump_schema_dumper(
                database_url,
                pg_dump_binary=args.pg_dump,
                container=args.pg_dump_container,
            ),
        )
    finally:
        connection.close()
    print(json.dumps(artifact.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
