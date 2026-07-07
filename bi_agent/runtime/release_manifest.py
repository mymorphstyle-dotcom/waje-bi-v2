from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_ROLLBACK_COMPONENTS = {
    "contract",
    "ledger",
    "capability_card",
    "prompt_recipe",
    "verifier_policy",
}

MANIFEST_PATH = Path(__file__).with_name("release_manifest.json")
REQUIRED_FIELDS = {
    "component",
    "paths",
    "active_ref",
    "rollback_ref",
    "owner",
    "required_checks",
    "rollback_action",
}


def load_release_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_release_manifest(manifest: dict[str, Any]) -> list[str]:
    components = manifest.get("components")
    if not isinstance(components, list):
        return ["components_missing"]

    problems: list[str] = []
    seen = set()
    for item in components:
        if not isinstance(item, dict):
            problems.append("component_not_mapping")
            continue
        missing = sorted(field for field in REQUIRED_FIELDS if not item.get(field))
        if missing:
            problems.append(f"{item.get('component', 'unknown')}:missing:{','.join(missing)}")
        seen.add(item.get("component"))

    missing_components = sorted(REQUIRED_ROLLBACK_COMPONENTS - seen)
    if missing_components:
        problems.append(f"missing_components:{','.join(missing_components)}")
    return problems


def rollback_plan(component: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = load_release_manifest() if manifest is None else manifest
    for item in manifest.get("components", []):
        if item.get("component") == component:
            return dict(item)
    raise KeyError(f"unknown rollback component: {component}")
