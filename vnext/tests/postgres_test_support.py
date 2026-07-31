from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path

import psycopg
from psycopg import sql

from waje_vnext.storage import (
    apply_gate1_migration,
    apply_gate2_migration,
    apply_gate3_1_migration,
    apply_gate3_2_migration,
    apply_gate3_4_migration,
)


ROOT = Path(__file__).resolve().parents[1]
MigrationApplier = Callable[..., str]
MIGRATIONS: tuple[tuple[MigrationApplier, Path], ...] = (
    (
        apply_gate1_migration,
        ROOT / "storage/migrations/001_gate1_authority.sql",
    ),
    (
        apply_gate2_migration,
        ROOT / "storage/migrations/002_gate2_controller.sql",
    ),
    (
        apply_gate3_1_migration,
        ROOT / "storage/migrations/003_gate3_1_measurement_authority.sql",
    ),
    (
        apply_gate3_2_migration,
        ROOT / "storage/migrations/004_gate3_2_runtime_sagas.sql",
    ),
    (
        apply_gate3_4_migration,
        ROOT / "storage/migrations/005_gate3_4_plan_query_continuity.sql",
    ),
)


def bootstrap_postgres_test_schema(dsn: str) -> None:
    """Apply every current migration in order and verify repeat application."""

    # A prior standalone Gate acceptance may have populated an older partial
    # schema. Test rows carry no migration value and may violate a newer FK
    # while the missing migration is being installed.
    reset_postgres_test_data(dsn)
    for apply_migration, migration_path in MIGRATIONS:
        first = apply_migration(dsn, migration_path=migration_path)
        second = apply_migration(dsn, migration_path=migration_path)
        if first != second:
            raise AssertionError(
                f"migration {migration_path.name} changed across repeat apply"
            )


def reset_postgres_test_data(dsn: str) -> None:
    """Clear test-owned rows while retaining the verified migration ledger."""

    reset_token = os.environ.get("WAJE_VNEXT_TEST_DATABASE_RESET_TOKEN", "")
    if (
        os.environ.get("WAJE_VNEXT_ALLOW_TEST_DATABASE_RESET") != "1"
        or not reset_token
    ):
        raise RuntimeError(
            "PostgreSQL acceptance reset requires the disposable Docker "
            "runner and its database-owned reset token"
        )
    with psycopg.connect(dsn) as connection:
        marker = connection.execute(
            """
            SELECT reset_token
            FROM public.waje_vnext_disposable_test_database_marker
            WHERE reset_token = %s
              AND database_name = current_database()
            """,
            (reset_token,),
        ).fetchone()
        if marker is None:
            raise RuntimeError(
                "PostgreSQL acceptance reset token is absent from the "
                "connected database; refusing destructive test cleanup"
            )
        rows = connection.execute(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = 'waje_vnext'
              AND tablename <> 'schema_migrations'
            ORDER BY tablename
            """
        ).fetchall()
        if not rows:
            return
        tables = sql.SQL(", ").join(
            sql.Identifier("waje_vnext", row[0]) for row in rows
        )
        connection.execute(
            sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                tables
            )
        )
