#!/usr/bin/env python3
"""Run Gate 1 PostgreSQL acceptance against an ephemeral Docker database."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("WAJE_VNEXT_POSTGRES_IMAGE", "postgres:17-alpine")
DATABASE_USER = "waje_vnext"
DATABASE_NAME = "waje_vnext"
DATABASE_PASSWORD = "gate1-ephemeral-password"


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


def _verify_epoch3_rejects_legacy_authority(dsn: str) -> None:
    sys.path.insert(
        0,
        str(ROOT / "services" / "analysis_core" / "src"),
    )
    from waje_vnext.storage import (  # noqa: PLC0415
        apply_gate1_migration,
        apply_gate3_1_migration,
    )

    migration_1 = ROOT / "storage/migrations/001_gate1_authority.sql"
    migration_3 = (
        ROOT / "storage/migrations/003_gate3_1_measurement_authority.sql"
    )
    apply_gate1_migration(dsn, migration_path=migration_1)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO waje_vnext.investigation_cases (
                case_id,
                thread_id,
                lifecycle,
                head_version,
                accepted_frame_revision_id,
                accepted_plan_revision_id,
                accepted_answer_version_id,
                opened_at,
                updated_at
            ) VALUES (
                'legacy-epoch-case',
                'legacy-thread',
                'open',
                0,
                NULL,
                NULL,
                NULL,
                now(),
                now()
            )
            """
        )
    try:
        apply_gate3_1_migration(dsn, migration_path=migration_3)
    except psycopg.errors.ObjectNotInPrerequisiteState as error:
        if "requires a clean waje_vnext authority schema" not in str(error):
            raise AssertionError(
                "epoch-3 migration rejected legacy data for the wrong reason"
            ) from error
    else:
        raise AssertionError(
            "epoch-3 migration accepted legacy authority data"
        )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA waje_vnext CASCADE")


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker is required for Gate 1 PostgreSQL acceptance")
    container_name = "waje-vnext-gate1-{}".format(uuid.uuid4().hex[:12])
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
        port = endpoint[1]
        dsn = "postgresql://{}:{}@127.0.0.1:{}/{}".format(
            DATABASE_USER,
            DATABASE_PASSWORD,
            port,
            DATABASE_NAME,
        )
        _wait_for_postgres(dsn)
        _verify_epoch3_rejects_legacy_authority(dsn)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "services" / "analysis_core" / "src"),
            "WAJE_VNEXT_DATABASE_URL": dsn,
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
                "test_gate1_postgres.py",
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
