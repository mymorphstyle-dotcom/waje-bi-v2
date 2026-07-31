#!/usr/bin/env python3
"""Run Gate 3.4 PostgreSQL acceptance against an ephemeral database."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("WAJE_VNEXT_POSTGRES_IMAGE", "postgres:17-alpine")
DATABASE_USER = "waje_vnext"
DATABASE_NAME = "waje_vnext"
DATABASE_PASSWORD = "gate3-4-ephemeral-password"


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        env=environment,
        text=True,
    )


def _wait_for_postgres(dsn: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2):
                return
        except psycopg.Error as error:
            last_error = error
            time.sleep(0.25)
    raise RuntimeError("ephemeral PostgreSQL did not become ready") from last_error


def _mark_disposable_database(dsn: str, reset_token: str) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            CREATE TABLE public.waje_vnext_disposable_test_database_marker (
                reset_token text PRIMARY KEY,
                database_name text NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO public.waje_vnext_disposable_test_database_marker (
                reset_token,
                database_name
            ) VALUES (%s, current_database())
            """,
            (reset_token,),
        )


def _verify_migration_transaction_atomicity(dsn: str) -> None:
    sys.path.insert(
        0,
        str(ROOT / "services" / "analysis_core" / "src"),
    )
    from waje_vnext.storage import (  # noqa: PLC0415
        apply_gate1_migration,
        apply_gate2_migration,
        apply_gate3_1_migration,
        apply_gate3_2_migration,
        apply_gate3_4_migration,
    )

    migrations = ROOT / "storage" / "migrations"
    apply_gate1_migration(
        dsn,
        migration_path=migrations / "001_gate1_authority.sql",
    )
    apply_gate2_migration(
        dsn,
        migration_path=migrations / "002_gate2_controller.sql",
    )
    apply_gate3_1_migration(
        dsn,
        migration_path=(
            migrations / "003_gate3_1_measurement_authority.sql"
        ),
    )

    def reject_ledger_version(version: int) -> None:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                CREATE OR REPLACE FUNCTION
                    public.fail_selected_migration_ledger_insert()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    IF NEW.version = TG_ARGV[0]::bigint THEN
                        RAISE EXCEPTION
                            'injected migration ledger failure';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
            connection.execute(
                sql.SQL(
                    """
                CREATE TRIGGER fail_selected_migration_ledger_insert
                BEFORE INSERT ON waje_vnext.schema_migrations
                FOR EACH ROW
                EXECUTE FUNCTION
                    public.fail_selected_migration_ledger_insert({})
                """
                ).format(sql.Literal(str(version)))
            )

    def clear_failure_trigger() -> None:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                DROP TRIGGER IF EXISTS
                    fail_selected_migration_ledger_insert
                ON waje_vnext.schema_migrations
                """
            )
            connection.execute(
                """
                DROP FUNCTION IF EXISTS
                    public.fail_selected_migration_ledger_insert()
                """
            )

    def assert_rolled_back(version: int, table_name: str) -> None:
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT
                    to_regclass(%s),
                    EXISTS (
                        SELECT 1
                        FROM waje_vnext.schema_migrations
                        WHERE version = %s
                    )
                """,
                (f"waje_vnext.{table_name}", version),
            ).fetchone()
        if row != (None, False):
            raise AssertionError(
                f"migration {version} escaped its outer transaction"
            )

    reject_ledger_version(4)
    try:
        try:
            apply_gate3_2_migration(
                dsn,
                migration_path=(
                    migrations / "004_gate3_2_runtime_sagas.sql"
                ),
            )
        except psycopg.errors.RaiseException:
            pass
        else:
            raise AssertionError(
                "migration 4 ignored the injected ledger failure"
            )
        assert_rolled_back(4, "frame_candidate_records")
    finally:
        clear_failure_trigger()

    apply_gate3_2_migration(
        dsn,
        migration_path=migrations / "004_gate3_2_runtime_sagas.sql",
    )
    reject_ledger_version(5)
    try:
        try:
            apply_gate3_4_migration(
                dsn,
                migration_path=(
                    migrations
                    / "005_gate3_4_plan_query_continuity.sql"
                ),
            )
        except psycopg.errors.RaiseException:
            pass
        else:
            raise AssertionError(
                "migration 5 ignored the injected ledger failure"
            )
        assert_rolled_back(5, "query_binding_envelopes")
    finally:
        clear_failure_trigger()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA waje_vnext CASCADE")


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError(
            "docker is required for Gate 3.4 PostgreSQL acceptance"
        )
    container_name = "waje-vnext-gate34-{}".format(uuid.uuid4().hex[:12])
    started = False
    try:
        _run(
            [
                docker,
                "run",
                "--rm",
                "--detach",
                "--name",
                container_name,
                "--tmpfs",
                "/var/lib/postgresql/data",
                "--env",
                "POSTGRES_USER={}".format(DATABASE_USER),
                "--env",
                "POSTGRES_PASSWORD={}".format(DATABASE_PASSWORD),
                "--env",
                "POSTGRES_DB={}".format(DATABASE_NAME),
                "--publish",
                "127.0.0.1::5432",
                IMAGE,
            ],
            capture_output=True,
        )
        started = True
        port_result = _run(
            [docker, "port", container_name, "5432/tcp"],
            capture_output=True,
        )
        endpoint = port_result.stdout.strip().rsplit(":", 1)
        if len(endpoint) != 2:
            raise RuntimeError("docker did not report the PostgreSQL port")
        dsn = "postgresql://{}:{}@127.0.0.1:{}/{}".format(
            DATABASE_USER,
            DATABASE_PASSWORD,
            endpoint[1],
            DATABASE_NAME,
        )
        _wait_for_postgres(dsn)
        reset_token = uuid.uuid4().hex
        _mark_disposable_database(dsn, reset_token)
        _verify_migration_transaction_atomicity(dsn)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": ":".join(
                (
                    str(ROOT / "services" / "analysis_core" / "src"),
                    str(ROOT / "tests"),
                )
            ),
            "WAJE_VNEXT_DATABASE_URL": dsn,
            "WAJE_VNEXT_ALLOW_TEST_DATABASE_RESET": "1",
            "WAJE_VNEXT_TEST_DATABASE_RESET_TOKEN": reset_token,
        }
        completed = _run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(ROOT / "tests"),
                "-p",
                "test_gate3_4_postgres.py",
                "-v",
            ],
            check=False,
            environment=environment,
        )
        return completed.returncode
    finally:
        if started:
            _run(
                [docker, "rm", "--force", container_name],
                check=False,
                capture_output=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
