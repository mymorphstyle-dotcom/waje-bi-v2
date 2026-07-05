from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from typing import Any


ROLE_VISIBILITY = {
    "business_reader": frozenset({"business_summary", "aggregate_evidence"}),
    "analyst": frozenset(
        {"business_summary", "aggregate_evidence", "diagnostic_detail"}
    ),
    "data_owner_admin": None,
}


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


def filter_artifact_for_role(artifact: Mapping[str, Any], role: str) -> dict[str, Any]:
    allowed = ROLE_VISIBILITY.get(role, ROLE_VISIBILITY["business_reader"])
    filtered = dict(artifact)
    if allowed is None:
        return to_jsonable(filtered)

    filtered["sections"] = [
        section
        for section in artifact.get("sections", [])
        if section.get("visibility") in allowed
    ]
    filtered.pop("admin_audit", None)
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
