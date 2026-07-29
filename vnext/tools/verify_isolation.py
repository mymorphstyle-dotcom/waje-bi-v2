#!/usr/bin/env python3
"""Verify the Day 0 boundary and run the vNext bootstrap from a clean copy."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "tools" / "isolation-policy.json"


@dataclass(frozen=True)
class Finding:
    check: str
    path: str
    detail: str


def _load_policy() -> Mapping[str, Any]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _directory_is_ignored(name: str, policy: Mapping[str, Any]) -> bool:
    if name in set(policy["ignored_directory_names"]):
        return True
    return any(
        fnmatch.fnmatch(name, pattern)
        for pattern in policy["ignored_directory_patterns"]
    )


def _walk_files(policy: Mapping[str, Any]) -> Iterable[Path]:
    for directory, directory_names, file_names in os.walk(ROOT):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not _directory_is_ignored(name, policy)
        )
        base = Path(directory)
        for file_name in sorted(file_names):
            yield base / file_name


def _check_required_paths(policy: Mapping[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    for relative_path in policy["required_paths"]:
        if not (ROOT / relative_path).is_file():
            findings.append(
                Finding("required_path", relative_path, "required file is missing")
            )
    return findings


def _check_python_toolchain(policy: Mapping[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    minimum = tuple(int(part) for part in policy["python"]["minimum"].split("."))
    current = sys.version_info[: len(minimum)]
    if current < minimum:
        findings.append(
            Finding(
                "python_toolchain",
                ".",
                "Python {}+ is required; verifier is running on {}.{}.{}".format(
                    policy["python"]["minimum"],
                    sys.version_info.major,
                    sys.version_info.minor,
                    sys.version_info.micro,
                ),
            )
        )
    pin_path = ROOT / ".python-version"
    if not pin_path.is_file():
        return findings
    pinned = pin_path.read_text(encoding="utf-8").strip()
    if pinned != policy["python"]["toolchain"]:
        findings.append(
            Finding(
                "python_toolchain",
                ".python-version",
                "toolchain pin {!r} must equal policy {!r}".format(
                    pinned, policy["python"]["toolchain"]
                ),
            )
        )
    if shutil.which("uv") is None:
        findings.append(
            Finding("python_toolchain", ".", "uv is required to recreate the venv")
        )
    return findings


def _check_symlinks(policy: Mapping[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    for directory, directory_names, file_names in os.walk(ROOT):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not _directory_is_ignored(name, policy)
        )
        base = Path(directory)
        for name in tuple(directory_names) + tuple(sorted(file_names)):
            path = base / name
            if not path.is_symlink():
                continue
            findings.append(
                Finding("symlink", _relative(path), "symlinks are forbidden in vNext")
            )
    return findings


def _is_scanned(path: Path, policy: Mapping[str, Any]) -> bool:
    relative_path = _relative(path)
    if relative_path in set(policy["excluded_relative_paths"]):
        return False
    return (
        path.suffix in set(policy["scan_extensions"])
        or path.name in set(policy["scan_file_names"])
    )


def _check_text_references(policy: Mapping[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    forbidden = tuple(policy["forbidden_references"])
    for path in _walk_files(policy):
        if not _is_scanned(path, policy):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(
                Finding("text_scan", _relative(path), "scanned file is not UTF-8")
            )
            continue
        for reference in forbidden:
            if reference in content:
                findings.append(
                    Finding(
                        "forbidden_reference",
                        _relative(path),
                        "contains {!r}".format(reference),
                    )
                )
    return findings


def _module_is_forbidden(module: str, forbidden_modules: Sequence[str]) -> bool:
    return any(
        module == forbidden or module.startswith(forbidden + ".")
        for forbidden in forbidden_modules
    )


def _check_python_imports(policy: Mapping[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    forbidden_modules = tuple(policy["forbidden_python_modules"])
    forbidden_literals = tuple(policy["forbidden_python_string_literals"])
    for path in _walk_files(policy):
        if path.suffix != ".py":
            continue
        relative_path = _relative(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
        except SyntaxError as error:
            findings.append(
                Finding("python_ast", relative_path, "syntax error: {}".format(error))
            )
            continue
        for node in ast.walk(tree):
            modules: Tuple[str, ...] = ()
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in forbidden_literals
            ):
                findings.append(
                    Finding(
                        "python_string_literal",
                        relative_path,
                        "contains forbidden dynamic-path literal {!r}".format(
                            node.value
                        ),
                    )
                )
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            for module in modules:
                if _module_is_forbidden(module, forbidden_modules):
                    findings.append(
                        Finding(
                            "python_import",
                            relative_path,
                            "imports forbidden module {!r}".format(module),
                        )
                    )
    return findings


def _manifest_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _manifest_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _manifest_strings(item)


def _check_node_manifests(policy: Mapping[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    forbidden_specifiers = tuple(policy["forbidden_manifest_specifiers"])
    for path in (ROOT / "package.json", ROOT / "package-lock.json"):
        if not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            findings.append(
                Finding("node_manifest", _relative(path), "invalid JSON: {}".format(error))
            )
            continue
        for value in _manifest_strings(manifest):
            for specifier in forbidden_specifiers:
                if specifier in value:
                    findings.append(
                        Finding(
                            "manifest_path_dependency",
                            _relative(path),
                            "contains forbidden path specifier {!r}".format(specifier),
                        )
                    )
    return findings


def _tree_sha256(policy: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    for path in sorted(_walk_files(policy), key=_relative):
        relative_path = _relative(path)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_environment(source_root: Path | None = None) -> Dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if source_root is not None:
        environment["PYTHONPATH"] = str(source_root)
    for optional_name in ("LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR"):
        if optional_name in os.environ:
            environment[optional_name] = os.environ[optional_name]
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> Mapping[str, Any]:
    completed = subprocess.run(
        list(command),
        capture_output=True,
        cwd=cwd,
        env=dict(environment),
        text=True,
    )
    return {
        "command": list(command),
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _check_wheel_artifact(
    wheel: Path,
    policy: Mapping[str, Any],
) -> List[Finding]:
    findings: List[Finding] = []
    allowed_top_levels = {
        "waje_vnext",
        "waje_bi_agent_vnext_analysis_core-0.0.0.dist-info",
    }
    forbidden = tuple(policy["forbidden_references"])
    with zipfile.ZipFile(wheel) as archive:
        members = tuple(archive.namelist())
        for member in members:
            member_path = Path(member)
            if member_path.is_absolute() or ".." in member_path.parts:
                findings.append(
                    Finding(
                        "wheel_artifact",
                        wheel.name,
                        "unsafe wheel member {!r}".format(member),
                    )
                )
                continue
            if member_path.parts and member_path.parts[0] not in allowed_top_levels:
                findings.append(
                    Finding(
                        "wheel_artifact",
                        wheel.name,
                        "unexpected top-level wheel member {!r}".format(member),
                    )
                )
            if Path(member).suffix not in {".json", ".py", ".sql", ".toml"}:
                continue
            try:
                content = archive.read(member).decode("utf-8")
            except UnicodeDecodeError:
                findings.append(
                    Finding(
                        "wheel_artifact",
                        wheel.name,
                        "scanned wheel member is not UTF-8: {!r}".format(member),
                    )
                )
                continue
            for reference in forbidden:
                if reference in content:
                    findings.append(
                        Finding(
                            "wheel_artifact",
                            wheel.name,
                            "member {!r} contains {!r}".format(member, reference),
                        )
                    )
        metadata_name = (
            "waje_bi_agent_vnext_analysis_core-0.0.0.dist-info/METADATA"
        )
        if metadata_name not in members:
            findings.append(
                Finding(
                    "wheel_artifact",
                    wheel.name,
                    "wheel metadata is missing",
                )
            )
        else:
            metadata = archive.read(metadata_name).decode("utf-8")
            if "Requires-Python: >=3.12" not in metadata:
                findings.append(
                    Finding(
                        "wheel_artifact",
                        wheel.name,
                        "wheel must require Python >=3.12",
                    )
                )
    return findings


def _run_clean_copy(
    policy: Mapping[str, Any],
) -> Tuple[
    List[Finding],
    List[Mapping[str, Any]],
    List[Mapping[str, Any]],
]:
    findings: List[Finding] = []
    commands: List[Mapping[str, Any]] = []
    artifacts: List[Mapping[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="waje-vnext-isolation-") as temporary:
        isolated_root = Path(temporary) / "vnext"
        shutil.copytree(
            ROOT,
            isolated_root,
            ignore=shutil.ignore_patterns(
                ".git",
                ".mypy_cache",
                ".next",
                ".pytest_cache",
                ".ruff_cache",
                ".venv",
                "__pycache__",
                "artifacts",
                "build",
                "coverage",
                "dist",
                "node_modules",
                "*.egg-info",
            ),
        )
        source_root = isolated_root / "services" / "analysis_core" / "src"
        wheel_root = Path(temporary) / "wheels"
        wheel_root.mkdir()
        uv_cache = Path(temporary) / "uv-cache"
        bootstrap_environment = _clean_environment()
        bootstrap_environment["UV_CACHE_DIR"] = str(uv_cache)
        bootstrap_environment["UV_PROJECT_ENVIRONMENT"] = str(
            isolated_root / ".venv"
        )
        uv_executable = shutil.which("uv")
        if uv_executable is None:
            return [
                Finding(
                    "clean_copy_toolchain",
                    ".",
                    "uv disappeared after the static toolchain check",
                )
            ], commands, artifacts
        sync_command = (
            uv_executable,
            "sync",
            "--frozen",
            "--no-install-project",
            "--python",
            policy["python"]["toolchain"],
        )
        sync_result = _run(
            sync_command,
            cwd=isolated_root,
            environment=bootstrap_environment,
        )
        commands.append(sync_result)
        if sync_result["exit_code"] != 0:
            return [
                Finding(
                    "clean_copy_toolchain",
                    ".",
                    "failed command: {}".format(" ".join(sync_command)),
                )
            ], commands, artifacts

        npm_executable = shutil.which("npm")
        if npm_executable is None:
            return [
                Finding(
                    "clean_copy_toolchain",
                    ".",
                    "npm is required to verify generated contract bindings",
                )
            ], commands, artifacts
        npm_environment = _clean_environment()
        npm_environment["npm_config_cache"] = str(Path(temporary) / "npm-cache")
        npm_install_command = (
            npm_executable,
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        )
        npm_install_result = _run(
            npm_install_command,
            cwd=isolated_root,
            environment=npm_environment,
        )
        commands.append(npm_install_result)
        if npm_install_result["exit_code"] != 0:
            return [
                Finding(
                    "clean_copy_toolchain",
                    ".",
                    "failed command: {}".format(
                        " ".join(npm_install_command)
                    ),
                )
            ], commands, artifacts
        contract_check_command = (
            npm_executable,
            "run",
            "check:contracts",
        )
        contract_check_result = _run(
            contract_check_command,
            cwd=isolated_root,
            environment=npm_environment,
        )
        commands.append(contract_check_result)
        if contract_check_result["exit_code"] != 0:
            findings.append(
                Finding(
                    "clean_copy_contracts",
                    ".",
                    "generated TypeScript contract bindings are stale",
                )
            )

        isolated_python = isolated_root / ".venv" / "bin" / "python"
        if not isolated_python.is_file():
            return [
                Finding(
                    "clean_copy_toolchain",
                    ".venv/bin/python",
                    "uv sync did not create the isolated Python executable",
                )
            ], commands, artifacts

        environment = _clean_environment(source_root)
        command_specs = (
            (
                str(isolated_python),
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheel_root),
            ),
            (
                str(isolated_python),
                "-m",
                "compileall",
                "-q",
                str(source_root),
                str(isolated_root / "tests"),
            ),
            (
                str(isolated_python),
                "-m",
                "unittest",
                "discover",
                "-s",
                str(isolated_root / "tests"),
                "-p",
                "test_*.py",
            ),
            (str(isolated_python), "--version"),
            (str(isolated_python), "-m", "waje_vnext", "health"),
        )
        for command in command_specs:
            result = _run(command, cwd=isolated_root, environment=environment)
            commands.append(result)
            if result["exit_code"] != 0:
                findings.append(
                    Finding(
                        "clean_copy_command",
                        ".",
                        "failed command: {}".format(" ".join(command)),
                    )
                )
        wheels = tuple(wheel_root.glob("waje_bi_agent_vnext_analysis_core-*.whl"))
        if len(wheels) != 1:
            findings.append(
                Finding(
                    "clean_copy_wheel",
                    ".",
                    "expected one correctly named analysis-core wheel, found {}".format(
                        len(wheels)
                    ),
                )
            )
        else:
            findings.extend(_check_wheel_artifact(wheels[0], policy))
            artifacts.append(
                {
                    "name": wheels[0].name,
                    "sha256": _file_sha256(wheels[0]),
                    "requires_python": ">=3.12",
                }
            )
        version_result = commands[-2] if len(commands) >= 2 else {}
        expected_version = "Python {}".format(policy["python"]["toolchain"])
        if version_result.get("stdout") != expected_version:
            findings.append(
                Finding(
                    "clean_copy_python_version",
                    ".venv/bin/python",
                    "expected {!r}, received {!r}".format(
                        expected_version, version_result.get("stdout")
                    ),
                )
            )
        if commands and commands[-1]["exit_code"] == 0:
            try:
                health = json.loads(str(commands[-1]["stdout"]))
            except json.JSONDecodeError as error:
                findings.append(
                    Finding(
                        "clean_copy_health",
                        ".",
                        "health output is invalid JSON: {}".format(error),
                    )
                )
            else:
                expected = {
                    "database_schema": "waje_vnext",
                    "environment_prefix": "WAJE_VNEXT_",
                    "python_namespace": "waje_vnext",
                    "status": "ok",
                }
                for key, value in expected.items():
                    if health.get(key) != value:
                        findings.append(
                            Finding(
                                "clean_copy_health",
                                ".",
                                "health {!r} must equal {!r}".format(key, value),
                            )
                        )
    return findings, commands, artifacts


def main() -> int:
    policy = _load_policy()
    findings_by_check = {
        "required_paths": _check_required_paths(policy),
        "python_toolchain": _check_python_toolchain(policy),
        "symlinks": _check_symlinks(policy),
        "text_references": _check_text_references(policy),
        "python_imports": _check_python_imports(policy),
        "node_manifests": _check_node_manifests(policy),
    }
    findings = [
        finding
        for check_findings in findings_by_check.values()
        for finding in check_findings
    ]

    command_results: List[Mapping[str, Any]] = []
    clean_copy_artifacts: List[Mapping[str, Any]] = []
    if not findings:
        (
            clean_copy_findings,
            command_results,
            clean_copy_artifacts,
        ) = _run_clean_copy(policy)
        findings_by_check["clean_copy"] = clean_copy_findings
        findings.extend(clean_copy_findings)
    else:
        findings_by_check["clean_copy"] = [
            Finding(
                "clean_copy",
                ".",
                "skipped because a static isolation check failed",
            )
        ]

    report = {
        "policy_version": policy["policy_version"],
        "status": "passed" if not findings else "failed",
        "implementation_root": str(ROOT),
        "tree_sha256": _tree_sha256(policy),
        "checks": {
            check_name: "failed" if check_findings else "passed"
            for check_name, check_findings in findings_by_check.items()
        },
        "clean_copy_commands": command_results,
        "clean_copy_artifacts": clean_copy_artifacts,
        "findings": [
            {"check": item.check, "path": item.path, "detail": item.detail}
            for item in findings
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
