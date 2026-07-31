#!/usr/bin/env python3
"""Validate Gate 3.5 migration in an ephemeral PostgreSQL container."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("WAJE_VNEXT_POSTGRES_IMAGE", "postgres:17-alpine")
DATABASE_USER = "waje_vnext"
DATABASE_NAME = "waje_vnext"
DATABASE_PASSWORD = "gate3-5-ephemeral-password"
MIGRATIONS = (
    (1, "gate1_authority", "001_gate1_authority.sql"),
    (2, "gate2_controller", "002_gate2_controller.sql"),
    (
        3,
        "gate3_1_measurement_authority",
        "003_gate3_1_measurement_authority.sql",
    ),
    (
        4,
        "gate3_2_runtime_sagas",
        "004_gate3_2_runtime_sagas.sql",
    ),
    (
        5,
        "gate3_4_plan_query_continuity",
        "005_gate3_4_plan_query_continuity.sql",
    ),
    (
        6,
        "gate3_5_evidence_answer_projection",
        "006_gate3_5_evidence_answer_projection.sql",
    ),
)


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


def _apply_migration(
    dsn: str,
    *,
    version: int,
    name: str,
    path: Path,
) -> str:
    migration_bytes = path.read_bytes()
    checksum = hashlib.sha256(migration_bytes).hexdigest()
    with psycopg.connect(dsn) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtext('waje_vnext_schema_migrations')
                )
                """
            )
            cursor.execute(
                "SELECT to_regclass('waje_vnext.schema_migrations')"
            )
            registry = cursor.fetchone()[0]
            if registry is not None:
                cursor.execute(
                    """
                    SELECT checksum_sha256
                    FROM waje_vnext.schema_migrations
                    WHERE version = %s
                    """,
                    (version,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[0] != checksum:
                        raise RuntimeError(
                            f"migration {version} checksum changed"
                        )
                    return checksum
            cursor.execute(migration_bytes.decode("utf-8"))
            cursor.execute(
                """
                INSERT INTO waje_vnext.schema_migrations (
                    version,
                    name,
                    checksum_sha256
                ) VALUES (%s, %s, %s)
                """,
                (version, name, checksum),
            )
    return checksum


def _reject_ledger_version(dsn: str, version: int) -> None:
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
                    RAISE EXCEPTION 'injected migration ledger failure';
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


def _clear_failure_trigger(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            DROP TRIGGER IF EXISTS fail_selected_migration_ledger_insert
            ON waje_vnext.schema_migrations
            """
        )
        connection.execute(
            """
            DROP FUNCTION IF EXISTS
                public.fail_selected_migration_ledger_insert()
            """
        )


def _verify_version6_atomicity(dsn: str) -> None:
    migration_root = ROOT / "storage" / "migrations"
    for version, name, filename in MIGRATIONS[:-1]:
        _apply_migration(
            dsn,
            version=version,
            name=name,
            path=migration_root / filename,
        )
    version, name, filename = MIGRATIONS[-1]
    _reject_ledger_version(dsn, version)
    try:
        try:
            _apply_migration(
                dsn,
                version=version,
                name=name,
                path=migration_root / filename,
            )
        except psycopg.errors.RaiseException:
            pass
        else:
            raise AssertionError(
                "migration 6 ignored the injected ledger failure"
            )
        with psycopg.connect(dsn) as connection:
            state = connection.execute(
                """
                SELECT
                    to_regclass(
                        'waje_vnext.capability_result_envelopes'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM waje_vnext.schema_migrations
                        WHERE version = 6
                    ),
                    to_regclass('waje_vnext.evidence_records')
                """
            ).fetchone()
        if state[:2] != (None, False) or state[2] is None:
            raise AssertionError(
                "migration 6 escaped its outer transaction"
            )
    finally:
        _clear_failure_trigger(dsn)
    first = _apply_migration(
        dsn,
        version=version,
        name=name,
        path=migration_root / filename,
    )
    second = _apply_migration(
        dsn,
        version=version,
        name=name,
        path=migration_root / filename,
    )
    if first != second:
        raise AssertionError("migration 6 repeat apply changed checksum")


def _verify_version6_rejects_superseded_rows(dsn: str) -> None:
    migration_root = ROOT / "storage" / "migrations"
    for version, name, filename in MIGRATIONS[:-1]:
        _apply_migration(
            dsn,
            version=version,
            name=name,
            path=migration_root / filename,
        )
    digest = "a" * 64
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO waje_vnext.investigation_cases (
                case_id,
                thread_id,
                lifecycle,
                head_version,
                analysis_cycle_id,
                opened_at,
                updated_at
            ) VALUES (
                'legacy-case',
                'legacy-thread',
                'open',
                0,
                'legacy-cycle',
                now(),
                now()
            )
            """
        )
        connection.execute(
            """
            INSERT INTO waje_vnext.question_revisions (
                question_revision_id,
                case_id,
                revision_number,
                prior_question_revision_id,
                analysis_cycle_id,
                accepted_head_version,
                content_sha256,
                payload,
                created_at
            ) VALUES (
                'legacy-question',
                'legacy-case',
                1,
                NULL,
                'legacy-cycle',
                1,
                %s,
                '{"schema_epoch": 3}'::jsonb,
                now()
            )
            """,
            (digest,),
        )
        connection.execute(
            """
            INSERT INTO waje_vnext.analysis_frame_revisions (
                frame_revision_id,
                case_id,
                revision_number,
                prior_frame_revision_id,
                content_sha256,
                payload,
                created_at,
                question_revision_id,
                schema_epoch,
                identity_algorithm_version,
                semantic_measurement_ids,
                authority_binding_ids
            ) VALUES (
                'legacy-frame',
                'legacy-case',
                1,
                NULL,
                %s,
                '{}'::jsonb,
                now(),
                'legacy-question',
                3,
                'measurement-identity.v1',
                ARRAY[%s],
                ARRAY[%s]
            )
            """,
            (digest, digest, digest),
        )
        connection.execute(
            """
            INSERT INTO waje_vnext.work_plan_revisions (
                plan_revision_id,
                case_id,
                frame_revision_id,
                revision_number,
                prior_plan_revision_id,
                content_sha256,
                payload,
                created_at
            ) VALUES (
                'legacy-plan',
                'legacy-case',
                'legacy-frame',
                1,
                NULL,
                %s,
                '{}'::jsonb,
                now()
            )
            """,
            (digest,),
        )
        connection.execute(
            """
            INSERT INTO waje_vnext.answer_versions (
                answer_version_id,
                case_id,
                frame_revision_id,
                plan_revision_id,
                version_number,
                prior_answer_version_id,
                status,
                content_sha256,
                payload,
                created_at
            ) VALUES (
                'legacy-answer',
                'legacy-case',
                'legacy-frame',
                'legacy-plan',
                1,
                NULL,
                'provisional',
                %s,
                '{"status": "provisional"}'::jsonb,
                now()
            )
            """,
            (digest,),
        )
    version, name, filename = MIGRATIONS[-1]
    try:
        _apply_migration(
            dsn,
            version=version,
            name=name,
            path=migration_root / filename,
        )
    except psycopg.Error as error:
        if error.sqlstate != "55000" or (
            "reset the disposable development database"
            not in str(error)
        ):
            raise AssertionError(
                "migration 6 rejected legacy rows with the wrong error"
            ) from error
    else:
        raise AssertionError("migration 6 accepted superseded answer rows")
    with psycopg.connect(dsn) as connection:
        state = connection.execute(
            """
            SELECT
                COUNT(*),
                EXISTS (
                    SELECT 1
                    FROM waje_vnext.schema_migrations
                    WHERE version = 6
                ),
                to_regclass(
                    'waje_vnext.capability_result_envelopes'
                )
            FROM waje_vnext.answer_versions
            """
        ).fetchone()
    if state != (1, False, None):
        raise AssertionError(
            "failed Gate 3.5 migration changed superseded authority"
        )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA waje_vnext CASCADE")


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError(
            "docker is required for Gate 3.5 migration acceptance"
        )
    container_name = "waje-vnext-gate35-{}".format(
        uuid.uuid4().hex[:12]
    )
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
                f"POSTGRES_USER={DATABASE_USER}",
                "--env",
                f"POSTGRES_PASSWORD={DATABASE_PASSWORD}",
                "--env",
                f"POSTGRES_DB={DATABASE_NAME}",
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
        _verify_version6_rejects_superseded_rows(dsn)
        _verify_version6_atomicity(dsn)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(
                (
                    str(ROOT),
                    str(ROOT / "services/analysis_core/src"),
                )
            ),
            "WAJE_VNEXT_GATE35_MIGRATION_TEST_DSN": dsn,
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
                "test_gate3_5_migration_contract.py",
                "-v",
            ],
            check=False,
            environment=environment,
        )
        storage_completed = _run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(ROOT / "tests"),
                "-p",
                "test_gate3_5_postgres_storage.py",
                "-v",
            ],
            check=False,
            environment=environment,
        )
        fault_matrix_completed = _run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(ROOT / "tests"),
                "-p",
                "test_gate3_5_fault_race_matrix.py",
                "-v",
            ],
            check=False,
            environment=environment,
        )
        return (
            completed.returncode
            or storage_completed.returncode
            or fault_matrix_completed.returncode
        )
    finally:
        if started:
            _run(
                [docker, "rm", "--force", container_name],
                check=False,
                capture_output=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
