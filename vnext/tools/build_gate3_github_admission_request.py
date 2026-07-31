#!/usr/bin/env python3
"""Build the content-addressed Gate 3 admission request on GitHub Actions."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from gate3_admission_authority import canonical_sha256
from github_gate3_admission import (
    GITHUB_REPOSITORY,
    GITHUB_REPOSITORY_ID,
    GITHUB_REPOSITORY_OWNER_ID,
    TRUSTED_ENVIRONMENT,
    TRUSTED_EVENT,
    TRUSTED_SOURCE_REF,
    TRUSTED_WORKFLOW_PATH,
    admission_authority_sha256,
)
from verify_gate3_e0 import (
    POLICY_PATH,
    build_admission_expectation,
)


ROOT = Path(__file__).resolve().parents[1]
REQUEST_SCHEMA_PATH = (
    ROOT / "evals" / "gate3" / "github-admission-request.schema.json"
)
CRITICAL_IMPORTS = (
    "jsonschema",
    "build_gate3_eval_corpus",
    "gate3_admission_authority",
    "github_gate3_admission",
    "verify_gate3_e0",
    "verify_github_workflow_deployment",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError("{} is required".format(name))
    return value


def _positive_integer_environment(name: str) -> int:
    value = _required_environment(name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError("{} must be an integer".format(name)) from error
    if parsed < 1:
        raise ValueError("{} must be positive".format(name))
    return parsed


def _sha256_list_environment(name: str) -> list[str]:
    raw_value = os.environ.get(name) or "[]"
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError("{} must be a JSON array".format(name)) from error
    if not isinstance(value, list):
        raise ValueError("{} must be a JSON array".format(name))
    if any(
        not isinstance(item, str)
        or len(item) != 64
        or any(character not in "0123456789abcdef" for character in item)
        for item in value
    ):
        raise ValueError(
            "{} must contain lowercase SHA-256 values".format(name)
        )
    if len(value) != len(set(value)):
        raise ValueError("{} cannot contain duplicates".format(name))
    return sorted(value)


def _assert_exact_environment(name: str, expected: str) -> str:
    actual = _required_environment(name)
    if actual != expected:
        raise ValueError(
            "{} must be {}, got {}".format(name, expected, actual)
        )
    return actual


def _dependency_inventory() -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        if not name:
            continue
        files: list[dict[str, str]] = []
        for relative_file in distribution.files or ():
            installed_file = Path(
                distribution.locate_file(relative_file)
            )
            if installed_file.is_symlink():
                raise ValueError(
                    "{} installed file is a symlink: {}".format(
                        name,
                        relative_file,
                    )
                )
            resolved_file = installed_file.resolve(strict=True)
            if not resolved_file.is_file():
                raise ValueError(
                    "{} installed file is missing: {}".format(
                        name,
                        relative_file,
                    )
                )
            files.append(
                {
                    "path": str(relative_file),
                    "sha256": _file_sha256(resolved_file),
                }
            )
        inventory.append(
            {
                "name": name.lower(),
                "version": distribution.version,
                "files": sorted(files, key=lambda item: item["path"]),
            }
        )
    return sorted(inventory, key=lambda item: (item["name"], item["version"]))


def _import_inventory() -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    root = ROOT.resolve()
    for module_name in CRITICAL_IMPORTS:
        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
        if not module_file:
            raise ValueError(
                "{} has no auditable import origin".format(module_name)
            )
        origin = Path(module_file).resolve()
        if not origin.is_file():
            raise ValueError(
                "{} import origin is not a file".format(module_name)
            )
        inventory.append(
            {
                "module": module_name,
                "origin": (
                    str(origin.relative_to(root))
                    if origin.is_relative_to(root)
                    else str(origin)
                ),
                "sha256": _file_sha256(origin),
            }
        )
    return inventory


def _runtime_attestation(
    evaluated_artifact_hashes: Mapping[str, str],
) -> dict[str, str]:
    executable = Path(sys.executable).resolve()
    python_version = platform.python_version()
    if not python_version.startswith("3.12."):
        raise ValueError(
            "Gate 3 admission requires Python 3.12.x, got {}".format(
                python_version
            )
        )
    node_version, node_hash = _tool_attestation("node")
    npm_version, npm_hash = _tool_attestation("npm")
    uv_version, uv_hash = _tool_attestation("uv")
    if node_version != "v22.18.0":
        raise ValueError(
            "Gate 3 admission requires Node v22.18.0, got {}".format(
                node_version
            )
        )
    if not npm_version.startswith("10."):
        raise ValueError(
            "Gate 3 admission requires npm 10.x, got {}".format(
                npm_version
            )
        )
    if uv_version != "uv 0.12.0":
        raise ValueError(
            "Gate 3 admission requires uv 0.12.0, got {}".format(
                uv_version
            )
        )
    return {
        "python_version": python_version,
        "python_executable_sha256": _file_sha256(executable),
        "node_version": node_version,
        "node_executable_sha256": node_hash,
        "npm_version": npm_version,
        "npm_executable_sha256": npm_hash,
        "uv_version": uv_version,
        "uv_executable_sha256": uv_hash,
        "dependency_inventory_sha256": canonical_sha256(
            _dependency_inventory()
        ),
        "import_inventory_sha256": canonical_sha256(_import_inventory()),
        "source_tree_sha256": canonical_sha256(
            dict(evaluated_artifact_hashes)
        ),
    }


def _tool_attestation(tool_name: str) -> tuple[str, str]:
    executable_name = shutil.which(tool_name)
    if executable_name is None:
        raise ValueError("{} executable is unavailable".format(tool_name))
    executable = Path(executable_name).resolve()
    if not executable.is_file():
        raise ValueError(
            "{} executable is not a file".format(tool_name)
        )
    completed = subprocess.run(
        [str(executable), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError(
            "{} version command failed".format(tool_name)
        )
    return completed.stdout.strip(), _file_sha256(executable)


def build_request() -> dict[str, Any]:
    repository = _assert_exact_environment(
        "GITHUB_REPOSITORY",
        GITHUB_REPOSITORY,
    )
    repository_id = _positive_integer_environment("GITHUB_REPOSITORY_ID")
    owner_id = _positive_integer_environment("GITHUB_REPOSITORY_OWNER_ID")
    if repository_id != GITHUB_REPOSITORY_ID:
        raise ValueError("GITHUB_REPOSITORY_ID is not trusted")
    if owner_id != GITHUB_REPOSITORY_OWNER_ID:
        raise ValueError("GITHUB_REPOSITORY_OWNER_ID is not trusted")
    source_ref = _assert_exact_environment("GITHUB_REF", TRUSTED_SOURCE_REF)
    event_name = _assert_exact_environment(
        "GITHUB_EVENT_NAME",
        TRUSTED_EVENT,
    )
    _assert_exact_environment(
        "WAJE_GATE3_ENVIRONMENT",
        TRUSTED_ENVIRONMENT,
    )
    workflow_ref = _required_environment("GITHUB_WORKFLOW_REF")
    expected_workflow_suffix = "{}@{}".format(
        TRUSTED_WORKFLOW_PATH,
        source_ref,
    )
    if not workflow_ref.endswith(expected_workflow_suffix):
        raise ValueError("GITHUB_WORKFLOW_REF is not the trusted workflow")
    source_revision = _required_environment("GITHUB_SHA")
    workflow_revision = _required_environment("GITHUB_WORKFLOW_SHA")
    if source_revision != workflow_revision:
        raise ValueError(
            "workflow revision must equal the protected source revision"
        )
    if len(source_revision) != 40 or any(
        character not in "0123456789abcdef"
        for character in source_revision
    ):
        raise ValueError("GITHUB_SHA must be a lowercase Git commit SHA")
    _assert_exact_environment("RUNNER_ENVIRONMENT", "github-hosted")
    run_id = _positive_integer_environment("GITHUB_RUN_ID")
    run_attempt = _positive_integer_environment("GITHUB_RUN_ATTEMPT")
    release_epoch = _positive_integer_environment(
        "GATE3_RELEASE_EPOCH"
    )
    trust_policy_epoch = _positive_integer_environment(
        "GATE3_TRUST_POLICY_EPOCH"
    )
    previous_admission = os.environ.get(
        "GATE3_PREVIOUS_ADMISSION_SHA256"
    )
    if previous_admission == "":
        previous_admission = None
    if previous_admission is not None and (
        len(previous_admission) != 64
        or any(
            character not in "0123456789abcdef"
            for character in previous_admission
        )
    ):
        raise ValueError(
            "GATE3_PREVIOUS_ADMISSION_SHA256 must be a SHA-256"
        )

    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    expectation = build_admission_expectation(policy)
    request: dict[str, Any] = {
        "artifact_type": "gate3_github_admission_request",
        "artifact_version": "gate3.github-admission-request.v1",
        "operation_id": "gate3-e0:{}:{}".format(run_id, run_attempt),
        "admission_authority_sha256": "0" * 64,
        "repository": {
            "name_with_owner": repository,
            "repository_id": repository_id,
            "repository_owner_id": owner_id,
            "source_revision": source_revision,
            "source_ref": source_ref,
            "event_name": event_name,
            "environment": TRUSTED_ENVIRONMENT,
        },
        "workflow": {
            "workflow_path": TRUSTED_WORKFLOW_PATH,
            "workflow_revision": workflow_revision,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "runner_environment": "github-hosted",
        },
        "release_authority": {
            "release_epoch": release_epoch,
            "trust_policy_epoch": trust_policy_epoch,
            "policy_sha256": expectation.policy_sha256,
            "authority_root_bundle_sha256": (
                expectation.authority_root_bundle_sha256
            ),
            "verifier_release_sha256": (
                expectation.verifier_release_sha256
            ),
            "evaluated_artifact_hashes": dict(
                expectation.evaluated_artifact_hashes
            ),
        },
        "runtime_attestation": _runtime_attestation(
            expectation.evaluated_artifact_hashes
        ),
        "authorization": {
            "authorized_attestation_sha256s": _sha256_list_environment(
                "GATE3_CANDIDATE_AUTHORIZED_ATTESTATION_SHA256S_JSON"
            ),
            "authorized_manifest_sha256s": _sha256_list_environment(
                "GATE3_CANDIDATE_AUTHORIZED_MANIFEST_SHA256S_JSON"
            ),
        },
        "previous_admission_sha256": previous_admission,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    request["admission_authority_sha256"] = admission_authority_sha256(
        request
    )
    return request


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".{}.".format(path.name),
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        request = build_request()
    except ValueError as error:
        print(
            json.dumps(
                {"status": "blocked", "findings": [str(error)]},
                ensure_ascii=False,
            )
        )
        return 1
    schema = json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    findings = [
        "{}: {}".format(
            "/".join(str(part) for part in error.absolute_path)
            or "<root>",
            error.message,
        )
        for error in Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(request)
    ]
    if findings:
        print(
            json.dumps(
                {"status": "blocked", "findings": findings},
                ensure_ascii=False,
            )
        )
        return 1
    _write_json_atomic(arguments.output, request)
    print(
        json.dumps(
            {
                "status": "candidate_created",
                "output": str(arguments.output),
                "admission_authority_sha256": request[
                    "admission_authority_sha256"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
