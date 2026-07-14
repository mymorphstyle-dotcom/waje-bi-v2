from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from bi_agent.runtime.answer_package_artifact import (
    build_answer_package_artifact_record,
)
from bi_agent.runtime.artifacts import synchronize_existing_artifact
from bi_agent.runtime.evidence_authority import canonical_digest


_ARTIFACT_DIRECTORY = TemporaryDirectory(
    prefix="waje-answer-package-authority-tests-"
)
_ARTIFACT_ROOT = Path(_ARTIFACT_DIRECTORY.name)


def materialize_answer_package_artifact(
    *,
    run_id: str,
    answer_package: Mapping[str, Any],
    artifact_path: str | Path | None = None,
) -> tuple[str, dict[str, Any]]:
    if str(answer_package.get("run_id") or "") != run_id:
        raise ValueError("test_answer_package_run_mismatch")
    artifact_path = Path(artifact_path) if artifact_path else _ARTIFACT_ROOT / (
        f"{canonical_digest(answer_package)}.json"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{}\n", encoding="utf-8")
    if not synchronize_existing_artifact(answer_package, artifact_path):
        raise AssertionError("test_answer_package_artifact_sync_failed")
    record = build_answer_package_artifact_record(
        run_id=run_id,
        artifact_path=artifact_path,
        answer_package=answer_package,
    )
    return str(artifact_path), record


def bind_answer_package_artifact(
    bundle: MutableMapping[str, Any],
    *,
    run_id: str,
    answer_package: Mapping[str, Any],
    artifact_path: str | Path | None = None,
) -> str:
    artifact_path, record = materialize_answer_package_artifact(
        run_id=run_id,
        answer_package=answer_package,
        artifact_path=artifact_path,
    )
    bundle["answer_package_artifacts"] = (record,)
    return artifact_path
