from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import zipfile

import pytest

from tools.runtime.backup_waje_runtime import (
    BACKUP_FORMAT,
    RuntimeBackupError,
    _publish_without_overwrite,
    create_runtime_backup,
    pg_dump_schema_dumper,
)
from tools.runtime.cutover_single_authority_schema import _validated_backup_ref
from tools.runtime.cutover_single_authority_schema import (
    SOURCE_MIGRATION_DIGEST,
    SOURCE_MIGRATION_ID,
)


class Result:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.rows = rows or []

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None


class CopyResult:
    def __init__(
        self,
        cursor: FakeCursor,
        chunks: tuple[bytes, ...],
        row_count: int,
    ) -> None:
        self.cursor = cursor
        self.chunks = chunks
        self.row_count = row_count

    def __enter__(self) -> CopyResult:
        return self

    def __exit__(self, *_args: object) -> None:
        self.cursor.rowcount = self.row_count

    def __iter__(self):
        return iter(self.chunks)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rowcount = -1

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def copy(self, statement: str) -> CopyResult:
        self.connection.copy_statements.append(statement)
        table = next(
            table
            for table in self.connection.table_payloads
            if f'."{table}" ' in statement
        )
        chunks, row_count = self.connection.table_payloads[table]
        return CopyResult(self, chunks, row_count)


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.copy_statements: list[str] = []
        self.rollback_count = 0
        self.table_payloads = {
            "run_dispatches": ((b"dispatch_id,payload\n", b'd-1,"a\nb"\n'), 1),
            "schema_migrations": (
                (
                    b"migration_id,migration_digest\n",
                    (
                        SOURCE_MIGRATION_ID + "," + SOURCE_MIGRATION_DIGEST + "\n"
                    ).encode(),
                ),
                1,
            ),
        }

    def execute(self, statement: str, params: object = None) -> Result:
        self.statements.append((statement, params))
        if "information_schema.tables" in statement:
            return Result([("run_dispatches",), ("schema_migrations",)])
        if "pg_export_snapshot" in statement:
            return Result([("00000003-0000001B-1",)])
        if "information_schema.columns" in statement:
            return Result(
                [
                    (
                        "run_dispatches",
                        "dispatch_id",
                        1,
                        "NO",
                        "text",
                        "text",
                        None,
                    )
                ]
            )
        if "FROM pg_constraint" in statement:
            return Result(
                [
                    (
                        "run_dispatches",
                        "run_dispatches_pkey",
                        "p",
                        "PRIMARY KEY (dispatch_id)",
                    )
                ]
            )
        if "FROM pg_indexes" in statement:
            return Result(
                [
                    (
                        "run_dispatches",
                        "run_dispatches_pkey",
                        "CREATE UNIQUE INDEX run_dispatches_pkey",
                    )
                ]
            )
        if "SELECT migration_id, migration_digest" in statement:
            return Result([(SOURCE_MIGRATION_ID, SOURCE_MIGRATION_DIGEST)])
        if "FROM pg_trigger" in statement:
            return Result(
                [
                    (
                        "run_dispatches",
                        "run_dispatches_immutable",
                        "CREATE TRIGGER run_dispatches_immutable",
                    )
                ]
            )
        return Result()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rollback_count += 1


def test_backup_is_one_read_only_snapshot_with_verified_streamed_members(
    tmp_path: Path,
) -> None:
    connection = FakeConnection()
    snapshots: list[str] = []
    schema = (
        b"CREATE SCHEMA waje_runtime;\nCREATE TABLE waje_runtime.run_dispatches();\n"
    )

    def dump(snapshot_id: str) -> bytes:
        snapshots.append(snapshot_id)
        return schema

    output = tmp_path / "waje-runtime-v7-before-v8.zip"
    artifact = create_runtime_backup(
        connection,
        output,
        schema_dumper=dump,
        timestamp=datetime(2026, 7, 20, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert artifact.path == output
    assert artifact.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert artifact.table_count == 2
    assert artifact.row_count == 2
    assert artifact.created_at == "2026-07-20T01:02:03Z"
    assert (
        _validated_backup_ref(
            {
                "single_authority_nonempty_tables": {"analysis_runs": 1},
                "obsolete_nonempty_tables": {},
                "dispatch_count": 1,
                "pre_v8_run_count": 1,
                "pending_clarification_thread_count": 0,
                "live_table_counts": {
                    "run_dispatches": 1,
                    "schema_migrations": 1,
                },
            },
            str(output),
        )
        == f"{output}#sha256={artifact.sha256}"
    )
    assert snapshots == ["00000003-0000001B-1"]
    assert connection.rollback_count == 1
    assert (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        in (connection.statements[0][0])
    )
    lock_index = next(
        index
        for index, (statement, _params) in enumerate(connection.statements)
        if statement.startswith("LOCK TABLE")
    )
    snapshot_index = next(
        index
        for index, (statement, _params) in enumerate(connection.statements)
        if "pg_export_snapshot" in statement
    )
    assert lock_index < snapshot_index
    assert connection.copy_statements == [
        'COPY "waje_runtime"."run_dispatches" TO STDOUT WITH '
        "(FORMAT CSV, HEADER TRUE, ENCODING 'UTF8')",
        'COPY "waje_runtime"."schema_migrations" TO STDOUT WITH '
        "(FORMAT CSV, HEADER TRUE, ENCODING 'UTF8')",
    ]

    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {
            "manifest.json",
            "schema/conversation-runtime.sql",
            "schema/live-schema-metadata.json",
            "tables/run_dispatches.csv",
            "tables/schema_migrations.csv",
        }
        assert archive.read("schema/conversation-runtime.sql") == schema
        assert archive.read("tables/run_dispatches.csv") == (
            b"dispatch_id,payload\n" + b'd-1,"a\nb"\n'
        )
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["backup_format"] == BACKUP_FORMAT
        assert manifest["created_at"] == "2026-07-20T01:02:03Z"
        assert manifest["schema"] == "waje_runtime"
        assert manifest["schema_contract_sha256"] == hashlib.sha256(schema).hexdigest()
        assert [record["table"] for record in manifest["tables"]] == [
            "run_dispatches",
            "schema_migrations",
        ]
        for record in manifest["tables"]:
            payload = archive.read(record["member"])
            assert record["uncompressed_bytes"] == len(payload)
            assert record["sha256"] == hashlib.sha256(payload).hexdigest()
            assert record["row_count"] == 1
        metadata = json.loads(archive.read("schema/live-schema-metadata.json"))
        assert metadata["migration_ledger"] == [
            {
                "migration_digest": SOURCE_MIGRATION_DIGEST,
                "migration_id": SOURCE_MIGRATION_ID,
            }
        ]
        assert metadata["columns"][0]["column"] == "dispatch_id"
        assert metadata["constraints"][0]["type"] == "p"
        assert metadata["indexes"][0]["name"] == "run_dispatches_pkey"
        assert metadata["triggers"][0]["name"] == "run_dispatches_immutable"


def test_schema_dump_failure_rolls_back_and_publishes_nothing(tmp_path: Path) -> None:
    connection = FakeConnection()
    output = tmp_path / "failed.zip"

    def fail(_snapshot_id: str) -> bytes:
        raise RuntimeBackupError("injected_schema_dump_failure")

    with pytest.raises(RuntimeBackupError, match="injected_schema_dump_failure"):
        create_runtime_backup(connection, output, schema_dumper=fail)

    assert connection.rollback_count == 1
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_output_is_rejected_before_database_access(tmp_path: Path) -> None:
    output = tmp_path / "existing.zip"
    output.write_bytes(b"keep-me")

    class NoDatabaseAccess:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("database must not be accessed")

    with pytest.raises(RuntimeBackupError, match="backup_output_exists"):
        create_runtime_backup(
            NoDatabaseAccess(),
            output,
            schema_dumper=lambda _snapshot: b"schema",
        )

    assert output.read_bytes() == b"keep-me"


def test_atomic_publish_race_never_overwrites_existing_target(tmp_path: Path) -> None:
    staged = tmp_path / "staged.tmp"
    target = tmp_path / "target.zip"
    staged.write_bytes(b"new-backup")
    target.write_bytes(b"existing-backup")

    with pytest.raises(RuntimeBackupError, match="backup_output_exists"):
        _publish_without_overwrite(staged, target)

    assert target.read_bytes() == b"existing-backup"
    assert staged.read_bytes() == b"new-backup"


def test_host_pg_dump_uses_exported_snapshot_without_secret_in_arguments() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    database_url = "postgresql://backup_user:do-not-print@db.internal:5432/runtime"

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=b"CREATE SCHEMA waje_runtime;\n",
            stderr=b"",
        )

    dump = pg_dump_schema_dumper(
        database_url,
        pg_dump_binary="/usr/local/bin/pg_dump",
        subprocess_run=run,
    )

    assert dump("00000003-0000001B-1") == b"CREATE SCHEMA waje_runtime;\n"
    command, kwargs = calls[0]
    assert command[0] == "/usr/local/bin/pg_dump"
    assert "--schema=waje_runtime" in command
    assert "--snapshot=00000003-0000001B-1" in command
    assert "--schema-only" in command
    assert "--no-owner" in command
    assert "--no-privileges" in command
    assert "do-not-print" not in " ".join(command)
    assert database_url not in " ".join(command)
    assert kwargs["env"]["PGDATABASE"] == database_url
    assert kwargs["check"] is False


def test_container_pg_dump_forwards_credentials_by_name_only() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    database_url = (
        "postgresql://backup_user:p%40ss@127.0.0.1:15432/"
        "waje_bi_runtime?sslmode=disable"
    )

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=b"CREATE SCHEMA waje_runtime;\n",
            stderr=b"",
        )

    dump = pg_dump_schema_dumper(
        database_url,
        container="waje-bi-postgres",
        subprocess_run=run,
    )
    dump("00000003-0000001B-1")

    command, kwargs = calls[0]
    assert command[:2] == ["docker", "exec"]
    assert "waje-bi-postgres" in command
    assert "--env" in command
    assert "PGDATABASE" in command
    assert "PGUSER" in command
    assert "PGPASSWORD" in command
    assert "PGSSLMODE" in command
    assert "p@ss" not in " ".join(command)
    assert "127.0.0.1" not in " ".join(command)
    assert kwargs["env"]["PGDATABASE"] == "waje_bi_runtime"
    assert kwargs["env"]["PGUSER"] == "backup_user"
    assert kwargs["env"]["PGPASSWORD"] == "p@ss"
    assert kwargs["env"]["PGSSLMODE"] == "disable"


def test_pg_dump_failure_does_not_expose_stderr_or_database_url() -> None:
    database_url = "postgresql://backup_user:do-not-print@db.internal/runtime"

    def run(_command: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=database_url.encode(),
        )

    dump = pg_dump_schema_dumper(database_url, subprocess_run=run)

    with pytest.raises(RuntimeBackupError) as failure:
        dump("00000003-0000001B-1")
    assert str(failure.value) == "pg_dump_failed"
    assert "do-not-print" not in str(failure.value)
