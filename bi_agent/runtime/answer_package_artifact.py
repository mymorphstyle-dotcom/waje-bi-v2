from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)


ANSWER_PACKAGE_ARTIFACT_SCHEMA_VERSION = "answer-package-artifact.v1"


def build_answer_package_artifact_record(
    *,
    run_id: str,
    artifact_path: str | Path,
    answer_package: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(run_id, str) or not run_id.strip():
        raise EvidenceIntegrityError("answer_package_artifact_run_invalid")
    path = Path(artifact_path).resolve(strict=True)
    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceIntegrityError("answer_package_artifact_payload_invalid") from exc
    expected = canonical_value(answer_package)
    if not isinstance(persisted, Mapping) or canonical_value(persisted) != expected:
        raise EvidenceIntegrityError("answer_package_artifact_payload_mismatch")
    if str(persisted.get("run_id") or "") != run_id:
        raise EvidenceIntegrityError("answer_package_artifact_run_mismatch")
    return canonical_value(
        {
            "schema_version": ANSWER_PACKAGE_ARTIFACT_SCHEMA_VERSION,
            "artifact_ref": f"answer-package:{run_id}",
            "run_id": run_id,
            "canonical_path": str(path),
            "payload_digest": canonical_digest(expected),
        }
    )


def validate_answer_package_artifact_record(
    record: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "artifact_ref",
        "run_id",
        "canonical_path",
        "payload_digest",
    }
    if not isinstance(record, Mapping) or set(record) != expected_fields:
        raise EvidenceIntegrityError("answer_package_artifact_record_invalid")
    payload = canonical_value(record)
    if (
        payload["schema_version"] != ANSWER_PACKAGE_ARTIFACT_SCHEMA_VERSION
        or payload["run_id"] != run_id
        or payload["artifact_ref"] != f"answer-package:{run_id}"
        or not isinstance(payload["canonical_path"], str)
        or not payload["canonical_path"]
        or not isinstance(payload["payload_digest"], str)
        or len(payload["payload_digest"]) != 64
    ):
        raise EvidenceIntegrityError("answer_package_artifact_record_invalid")
    return dict(payload)


def replacement_answer_package_artifact_ref(
    record: Mapping[str, Any],
) -> str:
    run_id = str(record.get("run_id") or "")
    digest = str(record.get("payload_digest") or "")
    if not run_id or len(digest) != 64:
        raise EvidenceIntegrityError("answer_package_artifact_record_invalid")
    return f"answer-package:{run_id}:replacement:{digest[:16]}"
