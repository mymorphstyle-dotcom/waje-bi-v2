from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


REQUIRED_ROLLBACK_COMPONENTS = {
    "contracts",
    "ledger",
    "conversation_and_plan_authority",
    "capability_execution_authority",
    "claim_and_narrative_authority",
    "publication_and_delivery",
    "gateway",
    "eval_governance",
}

MANIFEST_PATH = Path(__file__).with_name("release_manifest.json")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_REF_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
ROLLBACK_REF_PATTERN = re.compile(r"git:[0-9a-f]{40}\Z")
REQUIRED_FIELDS = {
    "component",
    "paths",
    "active_ref",
    "rollback_ref",
    "owner",
    "required_checks",
    "rollback_action",
}
PRODUCTION_INVENTORY_ROOTS = (
    "app",
    "bi_agent/capabilities",
    "bi_agent/conversation",
    "bi_agent/runtime",
    "contracts",
    "ops",
    "tools",
)
PRODUCTION_INVENTORY_FILES = ("compose.clickhouse.yaml",)
INVENTORY_EXCLUDED_PATHS = frozenset(
    {
        "bi_agent/runtime/release_manifest.json",
        "contracts/README.md",
        "tools/__init__.py",
        "tools/phase4/__init__.py",
        "tools/phase7/__init__.py",
    }
)
FORBIDDEN_LEGACY_PATHS = frozenset(
    {
        "app/api/artifacts/[artifactId]/continue/route.ts",
        "app/api/artifacts/[artifactId]/export/route.ts",
        "app/api/artifacts/[artifactId]/route.ts",
        "app/api/replays/route.ts",
        "app/api/runs/[runId]/retry/route.ts",
        "bi_agent/conversation/clarification_authority.py",
        "bi_agent/conversation/clarification_options.py",
        "bi_agent/runtime/analysis_assets.py",
        "bi_agent/runtime/analysis_obligations.py",
        "bi_agent/runtime/answer_package.py",
        "bi_agent/runtime/answer_package_artifact.py",
        "bi_agent/runtime/artifacts.py",
        "bi_agent/runtime/capability_harness.py",
        "bi_agent/runtime/capability_models.py",
        "bi_agent/runtime/capability_registry.py",
        "bi_agent/runtime/claim_provenance.py",
        "bi_agent/runtime/clickhouse_query_planner.py",
        "bi_agent/runtime/clickhouse_revenue_rows.py",
        "bi_agent/runtime/compiler.py",
        "bi_agent/runtime/data_contract_diagnostics.py",
        "bi_agent/runtime/diagnostic_insights.py",
        "bi_agent/runtime/exploration_budget.py",
        "bi_agent/runtime/final_narrative_binding.py",
        "bi_agent/runtime/formula_candidates.py",
        "bi_agent/runtime/formula_claim_numbers.py",
        "bi_agent/runtime/models.py",
        "bi_agent/runtime/query_repair.py",
        "bi_agent/runtime/recipe_registry.py",
        "bi_agent/runtime/reuse_decision.py",
        "bi_agent/runtime/revenue_runtime_plan.py",
        "bi_agent/runtime/runtime_publication_index.py",
        "bi_agent/runtime/wording.py",
    }
)
FORBIDDEN_LEGACY_SYMBOLS = frozenset(
    {
        "build_pattern_graph",
        "choice_actions",
        "phase4-draft",
        "recommended_assumption",
        "recommended_choice_id",
        "run_pattern_workflow",
    }
)
LEGACY_SYMBOL_SCAN_SUFFIXES = frozenset(
    {".cjs", ".js", ".jsx", ".mjs", ".py", ".ts", ".tsx"}
)


class ManifestPathError(ValueError):
    def __init__(self, kind: str, path: str) -> None:
        super().__init__(f"{kind}: {path}")
        self.kind = kind
        self.path = path


def _relative_manifest_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ManifestPathError("path_invalid", str(value))
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
    ):
        raise ManifestPathError("path_invalid", value)
    return path


def _is_generated_path(path: Path, *, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(
        part == "__pycache__" or part.startswith(".") for part in relative.parts
    ) or (path.suffix == ".pyc")


def _authoritative_files(
    paths: list[str], *, root: Path
) -> tuple[tuple[str, Path], ...]:
    root = root.resolve()
    files: dict[str, Path] = {}
    for raw_path in paths:
        relative = _relative_manifest_path(raw_path)
        target = root.joinpath(*relative.parts)
        if not target.exists():
            raise ManifestPathError("path_missing", raw_path)
        try:
            target.resolve().relative_to(root)
        except ValueError as exc:
            raise ManifestPathError("path_outside_root", raw_path) from exc

        candidates = (target,) if target.is_file() else target.rglob("*")
        matched = False
        for candidate in candidates:
            if not candidate.is_file() or _is_generated_path(candidate, root=root):
                continue
            try:
                candidate.resolve().relative_to(root)
            except ValueError as exc:
                raise ManifestPathError("path_outside_root", raw_path) from exc
            candidate_relative = candidate.relative_to(root).as_posix()
            files[candidate_relative] = candidate
            matched = True
        if not matched:
            raise ManifestPathError("path_contains_no_authoritative_files", raw_path)
    return tuple(sorted(files.items()))


def active_ref_for_paths(paths: list[str], *, root: Path = PROJECT_ROOT) -> str:
    """Return a content-addressed ref for the authoritative files under ``paths``."""

    digest = hashlib.sha256()
    digest.update(b"waje-release-component-v1\0")
    for relative_path, path in _authoritative_files(paths, root=root):
        path_bytes = relative_path.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(path_bytes).to_bytes(8, byteorder="big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, byteorder="big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _production_inventory(*, root: Path) -> frozenset[str]:
    root = root.resolve()
    inventory: set[str] = set()
    for raw_path in PRODUCTION_INVENTORY_ROOTS:
        target = root / raw_path
        if not target.exists():
            continue
        for candidate in target.rglob("*"):
            if not candidate.is_file() or _is_generated_path(candidate, root=root):
                continue
            relative = candidate.relative_to(root).as_posix()
            if relative not in INVENTORY_EXCLUDED_PATHS:
                inventory.add(relative)
    for raw_path in PRODUCTION_INVENTORY_FILES:
        candidate = root / raw_path
        if (
            candidate.is_file()
            and not _is_generated_path(candidate, root=root)
            and raw_path not in INVENTORY_EXCLUDED_PATHS
        ):
            inventory.add(raw_path)
    return frozenset(inventory)


def load_release_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_release_manifest(
    manifest: dict[str, Any],
    *,
    root: Path = PROJECT_ROOT,
) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest_not_mapping"]
    if (
        not isinstance(manifest.get("manifest_version"), str)
        or not manifest["manifest_version"].strip()
    ):
        return ["manifest_version_missing"]
    components = manifest.get("components")
    if not isinstance(components, list):
        return ["components_missing"]

    problems: list[str] = []
    seen: set[str] = set()
    covered_files: set[str] = set()
    for item in components:
        if not isinstance(item, dict):
            problems.append("component_not_mapping")
            continue
        missing = sorted(field for field in REQUIRED_FIELDS if not item.get(field))
        if missing:
            problems.append(
                f"{item.get('component', 'unknown')}:missing:{','.join(missing)}"
            )
        component = item.get("component")
        if not isinstance(component, str) or not component:
            continue
        if component in seen:
            problems.append(f"duplicate_component:{component}")
        seen.add(component)

        paths = item.get("paths")
        paths_valid = (
            isinstance(paths, list)
            and bool(paths)
            and all(isinstance(path, str) and path for path in paths)
        )
        active_ref = item.get("active_ref")
        active_ref_valid = isinstance(active_ref, str) and bool(
            ACTIVE_REF_PATTERN.fullmatch(active_ref)
        )
        if not active_ref_valid:
            problems.append(f"{component}:active_ref_invalid")
        if not paths_valid:
            problems.append(f"{component}:paths_invalid")
        else:
            try:
                expected_active_ref = active_ref_for_paths(paths, root=root)
                covered_files.update(
                    relative_path
                    for relative_path, _ in _authoritative_files(paths, root=root)
                )
            except ManifestPathError as exc:
                problems.append(f"{component}:{exc.kind}:{exc.path}")
            else:
                if active_ref_valid and active_ref != expected_active_ref:
                    problems.append(
                        f"{component}:active_ref_mismatch:expected={expected_active_ref}"
                    )

        rollback_ref = item.get("rollback_ref")
        if not isinstance(rollback_ref, str) or not ROLLBACK_REF_PATTERN.fullmatch(
            rollback_ref
        ):
            problems.append(f"{component}:rollback_ref_invalid")

    missing_components = sorted(REQUIRED_ROLLBACK_COMPONENTS - seen)
    if missing_components:
        problems.append(f"missing_components:{','.join(missing_components)}")
    unknown_components = sorted(seen - REQUIRED_ROLLBACK_COMPONENTS)
    if unknown_components:
        problems.append(f"unknown_components:{','.join(unknown_components)}")
    for path in sorted(FORBIDDEN_LEGACY_PATHS):
        if (root / path).exists():
            problems.append(f"forbidden_legacy_path:{path}")
    production_inventory = _production_inventory(root=root)
    for path in sorted(production_inventory):
        if (
            path == "bi_agent/runtime/release_manifest.py"
            or PurePosixPath(path).suffix not in LEGACY_SYMBOL_SCAN_SUFFIXES
        ):
            continue
        source = (root / path).read_text(encoding="utf-8")
        for symbol in sorted(FORBIDDEN_LEGACY_SYMBOLS):
            if symbol in source:
                problems.append(f"forbidden_legacy_symbol:{symbol}:{path}")
    for path in sorted(production_inventory - covered_files):
        problems.append(f"uncovered_production_path:{path}")
    return problems


def rollback_plan(
    component: str, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    manifest = load_release_manifest() if manifest is None else manifest
    for item in manifest.get("components", []):
        if item.get("component") == component:
            return dict(item)
    raise KeyError(f"unknown rollback component: {component}")
