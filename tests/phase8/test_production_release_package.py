from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from tools.runtime.build_production_release import (
    ProductionReleaseError,
    _is_forbidden_release_source,
    _normalized_tar_info,
    _runtime_copy_ignore,
    _validated_deployment_report,
)


@pytest.mark.parametrize(
    "path",
    (
        ".env",
        ".env.production",
        "nested/.env.local",
        "artifacts/report.json",
        "data/raw/orders.csv",
        "dist/releases/build.tar.gz",
        "node_modules/next/index.js",
        ".venv/bin/python",
        ".next/server/app.js",
    ),
)
def test_release_source_rejects_local_state_and_secret_paths(path: str) -> None:
    assert _is_forbidden_release_source(path)


@pytest.mark.parametrize(
    "path",
    (
        "app/page.tsx",
        "bi_agent/runtime/agent_turn_runtime.py",
        "contracts/runtime/clickhouse-analysis-bindings.yaml",
        "tools/runtime/recover_run_dispatches.py",
        "package-lock.json",
    ),
)
def test_release_source_accepts_authoritative_runtime_paths(path: str) -> None:
    assert not _is_forbidden_release_source(path)


def test_deployment_report_must_be_passed_and_secret_free(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schemaVersion": "general-agent-deployment.v1",
                "status": "passed",
                "checks": [],
            }
        ),
        encoding="utf-8",
    )
    assert _validated_deployment_report(report)["status"] == "passed"

    report.write_text(
        json.dumps(
            {
                "schemaVersion": "general-agent-deployment.v1",
                "status": "passed",
                "apiKey": "must-not-ship",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ProductionReleaseError,
        match="deployment_report_forbidden_secret_field",
    ):
        _validated_deployment_report(report)


def test_archive_metadata_is_normalized() -> None:
    info = tarfile.TarInfo("release/server.js")
    info.uid = 501
    info.gid = 20
    info.uname = "developer"
    info.gname = "staff"
    info.mode = 0o775
    info.mtime = 123456
    info.type = tarfile.REGTYPE

    normalized = _normalized_tar_info(info)

    assert normalized.uid == 0
    assert normalized.gid == 0
    assert normalized.uname == "root"
    assert normalized.gname == "root"
    assert normalized.mode == 0o755
    assert normalized.mtime == 0


def test_next_standalone_copy_always_excludes_env_files() -> None:
    ignored = _runtime_copy_ignore(
        "/tmp/standalone",
        [".env", ".env.production", "server.js", ".next"],
    )

    assert ignored == {".env", ".env.production"}
