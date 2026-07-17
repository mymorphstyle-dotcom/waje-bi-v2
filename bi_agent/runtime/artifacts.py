from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any


CUSTOMER_SAFE_VISIBILITIES = frozenset(
    {"business_summary", "aggregate_evidence", "diagnostic_detail"}
)


def persist_artifact(
    artifact: Mapping[str, Any],
    *,
    artifact_root: str = "artifacts/phase-4",
) -> str:
    run_id = artifact["run_id"]
    output_dir = Path(artifact_root) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "answer_package.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(to_jsonable(artifact), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return str(output_path)


def synchronize_existing_artifact(
    artifact: Mapping[str, Any],
    artifact_path: str | Path | None,
) -> bool:
    if not artifact_path:
        return False
    output_path = Path(artifact_path)
    if not output_path.is_file():
        return False
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(
                to_jsonable(artifact),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output_path)
        temp_path = None
        return True
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def filter_customer_safe_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    filtered = {
        "run_id": artifact.get("run_id"),
        "status": artifact.get("status"),
        "package_type": artifact.get("package_type"),
        "sections": [
            section
            for section in artifact.get("sections", [])
            if section.get("visibility") in CUSTOMER_SAFE_VISIBILITIES
        ],
    }
    return to_jsonable(filtered)


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
