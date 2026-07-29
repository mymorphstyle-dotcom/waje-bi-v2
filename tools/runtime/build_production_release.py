from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from bi_agent.runtime.release_manifest import (
    MANIFEST_PATH,
    PROJECT_ROOT,
    load_release_manifest,
    validate_release_manifest,
)


RELEASE_SCHEMA_VERSION = "waje-production-release.v1"
RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SOURCE_DATE_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)\Z"
)
FORBIDDEN_SOURCE_ROOTS = frozenset(
    {
        ".git",
        ".next",
        ".venv",
        "artifacts",
        "build",
        "coverage",
        "data/clickhouse",
        "data/local",
        "data/postgres",
        "data/raw",
        "dist",
        "node_modules",
        "output",
        "playwright-report",
        "test-results",
        "tmp",
    }
)
FORBIDDEN_REPORT_KEY_PARTS = frozenset(
    {
        "apikey",
        "api_key",
        "authorization",
        "password",
        "rawpayload",
        "raw_payload",
        "secret",
    }
)


class ProductionReleaseError(RuntimeError):
    pass


def _run(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ProductionReleaseError(f"command_failed:{args[0]}:{args[1]}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProductionReleaseError(f"release_source_path_invalid:{value}")
    return path


def _is_forbidden_release_source(value: str) -> bool:
    path = _relative_path(value)
    if any(part == ".env" or part.startswith(".env.") for part in path.parts):
        return True
    path_text = path.as_posix()
    return any(
        path_text == root or path_text.startswith(root + "/")
        for root in FORBIDDEN_SOURCE_ROOTS
    )


def _tracked_files() -> tuple[PurePosixPath, ...]:
    output = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    paths = tuple(
        sorted(
            (
                _relative_path(raw.decode("utf-8"))
                for raw in output.split(b"\0")
                if raw
            ),
            key=lambda item: item.as_posix(),
        )
    )
    forbidden = tuple(
        path.as_posix()
        for path in paths
        if _is_forbidden_release_source(path.as_posix())
    )
    if forbidden:
        raise ProductionReleaseError(
            "forbidden_tracked_release_sources:" + ",".join(forbidden)
        )
    return paths


def _copy_tracked_sources(destination: Path) -> None:
    for relative in _tracked_files():
        source = PROJECT_ROOT.joinpath(*relative.parts)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def _safe_symlink(path: Path, *, root: Path) -> bool:
    if not path.is_symlink():
        return True
    target = os.readlink(path)
    if os.path.isabs(target):
        return False
    try:
        (path.parent / target).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _assert_safe_runtime_tree(root: Path) -> None:
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        relative_path = _relative_path(relative)
        if any(
            part == ".env" or part.startswith(".env.")
            for part in relative_path.parts
        ):
            raise ProductionReleaseError(
                f"forbidden_runtime_release_source:{relative}"
            )
        if path.is_symlink() and not _safe_symlink(path, root=root):
            raise ProductionReleaseError(f"unsafe_runtime_symlink:{relative}")


def _runtime_copy_ignore(_: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == ".env" or name.startswith(".env.")
    }


def _copy_next_standalone(destination: Path) -> str:
    standalone = PROJECT_ROOT / ".next" / "standalone"
    build_id = PROJECT_ROOT / ".next" / "BUILD_ID"
    server = standalone / "server.js"
    if not server.is_file() or not build_id.is_file():
        raise ProductionReleaseError(
            "next_standalone_build_missing:run_npm_build"
        )
    for path in standalone.rglob("*"):
        if path.is_symlink() and not _safe_symlink(path, root=standalone):
            raise ProductionReleaseError(
                "unsafe_runtime_symlink:"
                + path.relative_to(standalone).as_posix()
            )
    shutil.copytree(
        standalone,
        destination,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=_runtime_copy_ignore,
    )
    static = PROJECT_ROOT / ".next" / "static"
    if not static.is_dir():
        raise ProductionReleaseError("next_static_build_missing")
    shutil.copytree(
        static,
        destination / ".next" / "static",
        dirs_exist_ok=True,
        symlinks=True,
    )
    public = PROJECT_ROOT / "public"
    if public.is_dir():
        shutil.copytree(
            public,
            destination / "public",
            dirs_exist_ok=True,
            symlinks=True,
        )
    _assert_safe_runtime_tree(destination)
    return build_id.read_text(encoding="utf-8").strip()


def _report_contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            if any(part in normalized for part in FORBIDDEN_REPORT_KEY_PARTS):
                return True
            if _report_contains_forbidden_key(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_report_contains_forbidden_key(item) for item in value)
    return False


def _validated_deployment_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionReleaseError("deployment_report_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != "general-agent-deployment.v1"
        or payload.get("status") != "passed"
    ):
        raise ProductionReleaseError("deployment_report_not_passed")
    if _report_contains_forbidden_key(payload):
        raise ProductionReleaseError("deployment_report_forbidden_secret_field")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _file_checksums(root: Path) -> tuple[str, ...]:
    return tuple(
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


def _write_deterministic_archive(
    *,
    release_root: Path,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_output,
            mtime=0,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
                dereference=False,
            ) as archive:
                paths: Iterable[Path] = (
                    release_root,
                    *sorted(release_root.rglob("*")),
                )
                for path in paths:
                    archive.add(
                        path,
                        arcname=path.relative_to(release_root.parent).as_posix(),
                        recursive=False,
                        filter=_normalized_tar_info,
                    )


def build_production_release(
    *,
    output_root: Path,
    deployment_report: Path,
    allow_dirty: bool,
) -> dict[str, Any]:
    manifest = load_release_manifest()
    problems = validate_release_manifest(manifest)
    if problems:
        raise ProductionReleaseError(
            "release_manifest_invalid:" + "|".join(problems)
        )
    release_id = manifest["manifest_version"]
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ProductionReleaseError("release_id_invalid")

    source_commit = _run("git", "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ProductionReleaseError("source_commit_invalid")
    status = _run("git", "status", "--porcelain=v1", "--untracked-files=all")
    if status and not allow_dirty:
        raise ProductionReleaseError("release_worktree_not_clean")
    source_date = _run("git", "show", "-s", "--format=%cI", source_commit)
    if not SOURCE_DATE_PATTERN.fullmatch(source_date):
        raise ProductionReleaseError("source_date_invalid")

    report = _validated_deployment_report(deployment_report)
    output_dir = output_root / release_id
    archive_path = output_dir / f"{release_id}.tar.gz"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="waje-production-release-") as tmp:
        release_root = Path(tmp) / release_id
        release_root.mkdir()
        _copy_tracked_sources(release_root)
        next_build_id = _copy_next_standalone(release_root)

        report_target = (
            release_root
            / "attestations"
            / "general-agent-deployment.json"
        )
        _write_json(report_target, report)
        metadata = {
            "schemaVersion": RELEASE_SCHEMA_VERSION,
            "releaseId": release_id,
            "sourceCommit": source_commit,
            "sourceDate": source_date,
            "sourceWorktree": "dirty" if status else "clean",
            "releaseManifestSha256": _sha256(MANIFEST_PATH),
            "nextBuildId": next_build_id,
            "componentRefs": {
                item["component"]: item["active_ref"]
                for item in manifest["components"]
            },
            "entrypoints": {
                "gateway": "node server.js",
                "recoveryWorker": (
                    "$WAJE_PYTHON_EXECUTABLE "
                    "-m tools.runtime.recover_run_dispatches"
                ),
                "tracePrune": (
                    "$WAJE_PYTHON_EXECUTABLE "
                    "-m tools.runtime.prune_agent_traces"
                ),
            },
            "deploymentReport": {
                "path": (
                    "attestations/general-agent-deployment.json"
                ),
                "sha256": _sha256(report_target),
                "schemaVersion": report["schemaVersion"],
                "status": report["status"],
            },
        }
        _write_json(release_root / "RELEASE.json", metadata)
        checksums = _file_checksums(release_root)
        (release_root / "RELEASE-FILES.sha256").write_text(
            "\n".join(checksums) + "\n",
            encoding="utf-8",
        )
        _write_deterministic_archive(
            release_root=release_root,
            output_path=archive_path,
        )

    archive_digest = _sha256(archive_path)
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    checksum_path.write_text(
        f"{archive_digest}  {archive_path.name}\n",
        encoding="utf-8",
    )
    handoff = {
        **metadata,
        "archive": {
            "path": archive_path.name,
            "sha256": archive_digest,
            "bytes": archive_path.stat().st_size,
        },
    }
    handoff_path = output_dir / f"{release_id}.release.json"
    _write_json(handoff_path, handoff)
    return {
        "releaseId": release_id,
        "archive": str(archive_path),
        "archiveSha256": archive_digest,
        "releaseMetadata": str(handoff_path),
        "status": "ready",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a content-addressed WAJE BI production release.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "dist" / "releases",
    )
    parser.add_argument(
        "--deployment-report",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Build a non-production candidate from a dirty worktree.",
    )
    args = parser.parse_args()
    result = build_production_release(
        output_root=args.output_root.resolve(),
        deployment_report=args.deployment_report.resolve(),
        allow_dirty=args.allow_dirty,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
