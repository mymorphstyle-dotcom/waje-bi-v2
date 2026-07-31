#!/usr/bin/env python3
"""Verify deployed GitHub workflows against vNext-owned authority policy."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
POLICY_PATH = (
    ROOT / "ops" / "github" / "workflow-authority-policy.json"
)
FULL_SHA_ACTION = re.compile(
    r"^(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"@(?P<revision>[a-f0-9]{40})$"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json_strict(path: Path) -> Any:
    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(
                    "duplicate JSON key {}".format(key)
                )
            value[key] = item
        return value

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
    )


def _load_workflow(path: Path) -> Mapping[str, Any]:
    value = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    if not isinstance(value, Mapping):
        raise ValueError("{} is not a workflow object".format(path))
    return value


def _all_steps(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]


def _validate_exact_jobs(
    workflow: Mapping[str, Any],
    *,
    expected_job_ids: set[str],
    privileged_job_id: str | None,
    label: str,
) -> list[str]:
    findings: list[str] = []
    jobs = workflow.get("jobs")
    if not isinstance(jobs, Mapping):
        return ["{} jobs are missing".format(label)]
    if set(jobs) != expected_job_ids:
        findings.append("{} job set does not match authority policy".format(label))
    for job_id, job in jobs.items():
        if not isinstance(job, Mapping):
            findings.append("{} job {} is invalid".format(label, job_id))
            continue
        if "uses" in job:
            findings.append(
                "{} job {} delegates to a reusable workflow".format(
                    label,
                    job_id,
                )
            )
        if job_id == privileged_job_id:
            continue
        if job.get("permissions", {"contents": "read"}) != {
            "contents": "read"
        }:
            findings.append(
                "{} job {} has privileged permissions".format(
                    label,
                    job_id,
                )
            )
        for step in job.get("steps", []):
            action = step.get("uses", "").split("@", 1)[0]
            if action == "actions/attest":
                findings.append(
                    "{} job {} invokes the attestation signer".format(
                        label,
                        job_id,
                    )
                )
    return findings


def _event_names(workflow: Mapping[str, Any]) -> set[str]:
    trigger = workflow.get("on")
    if not isinstance(trigger, Mapping):
        return set()
    return set(trigger)


def _validate_repository_execution_boundary(
    workflow: Mapping[str, Any],
    *,
    privileged_job_id: str | None,
    label: str,
) -> list[str]:
    findings: list[str] = []
    forbidden_run_fragments = (
        "../",
        "${{ github.workspace }}",
        "$GITHUB_WORKSPACE",
    )
    for job_id, job in workflow["jobs"].items():
        steps = job.get("steps", [])
        for step in steps:
            action = step.get("uses", "")
            if action.startswith("./") or action.startswith("../"):
                findings.append(
                    "{} job {} uses a repository-local action".format(
                        label,
                        job_id,
                    )
                )
        if job_id == privileged_job_id:
            continue
        run_steps = [step for step in steps if "run" in step]
        if not run_steps:
            continue
        working_directory = (
            job.get("defaults", {})
            .get("run", {})
            .get("working-directory")
        )
        if working_directory != "vnext":
            findings.append(
                "{} job {} run boundary is not vnext".format(
                    label,
                    job_id,
                )
            )
        for step in run_steps:
            if "working-directory" in step:
                findings.append(
                    "{} job {} overrides the vnext run boundary".format(
                        label,
                        job_id,
                    )
                )
            command = step["run"]
            if any(
                fragment in command
                for fragment in forbidden_run_fragments
            ):
                findings.append(
                    "{} job {} run step escapes the vnext boundary".format(
                        label,
                        job_id,
                    )
                )
    return findings


def _validate_action_pins(
    workflow: Mapping[str, Any],
    approved_actions: Mapping[str, str],
    *,
    label: str,
) -> list[str]:
    findings: list[str] = []
    for step in _all_steps(workflow):
        action = step.get("uses")
        if action is None:
            continue
        match = FULL_SHA_ACTION.fullmatch(action)
        if match is None:
            findings.append(
                "{} action is not pinned to a full SHA: {}".format(
                    label,
                    action,
                )
            )
            continue
        action_name = match.group("action")
        if approved_actions.get(action_name) != match.group("revision"):
            findings.append(
                "{} action is not approved: {}".format(label, action)
            )
    return findings


def verify_deployment(
    policy: Mapping[str, Any],
    *,
    workspace_root: Path = WORKSPACE_ROOT,
) -> list[str]:
    findings: list[str] = []
    root = workspace_root.resolve()
    deployed: dict[str, Path] = {}
    for reference, expected_hash in policy["deployment_files"].items():
        candidate = root / reference
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            findings.append(
                "deployment path is missing or escapes workspace: {}".format(
                    reference
                )
            )
            continue
        deployed[reference] = candidate
        if (
            expected_hash == "PLACEHOLDER"
            or len(expected_hash) != 64
        ):
            findings.append(
                "deployment hash is unprovisioned: {}".format(reference)
            )
            continue
        if candidate.is_symlink() or not candidate.is_file():
            findings.append(
                "deployment file is missing or unsafe: {}".format(reference)
            )
        elif _sha256(candidate) != expected_hash:
            findings.append(
                "deployment file hash is stale: {}".format(reference)
            )
    if findings:
        return findings

    ci_path = deployed[".github/workflows/vnext-ci.yml"]
    protected_path = deployed[
        ".github/workflows/gate3-protected-admission.yml"
    ]
    ci = _load_workflow(ci_path)
    protected = _load_workflow(protected_path)
    approved_actions = policy["approved_actions"]
    workflow_job_ids = policy["workflow_job_ids"]
    privileged_policy = policy["privileged_job"]
    findings.extend(
        _validate_exact_jobs(
            ci,
            expected_job_ids=set(
                workflow_job_ids[".github/workflows/vnext-ci.yml"]
            ),
            privileged_job_id=None,
            label=ci_path.name,
        )
    )
    findings.extend(
        _validate_exact_jobs(
            protected,
            expected_job_ids=set(
                workflow_job_ids[
                    ".github/workflows/gate3-protected-admission.yml"
                ]
            ),
            privileged_job_id=privileged_policy["job_id"],
            label=protected_path.name,
        )
    )
    findings.extend(
        _validate_action_pins(
            ci,
            approved_actions,
            label=ci_path.name,
        )
    )
    findings.extend(
        _validate_action_pins(
            protected,
            approved_actions,
            label=protected_path.name,
        )
    )
    findings.extend(
        _validate_repository_execution_boundary(
            ci,
            privileged_job_id=None,
            label=ci_path.name,
        )
    )
    findings.extend(
        _validate_repository_execution_boundary(
            protected,
            privileged_job_id=privileged_policy["job_id"],
            label=protected_path.name,
        )
    )

    forbidden_events = set(policy["forbidden_events"])
    ci_events = _event_names(ci)
    protected_events = _event_names(protected)
    if ci_events != set(policy["unprivileged_events"]):
        findings.append("vNext CI events do not match authority policy")
    if protected_events != set(policy["protected_events"]):
        findings.append(
            "protected admission events do not match authority policy"
        )
    if forbidden_events & (ci_events | protected_events):
        findings.append("a forbidden GitHub event is configured")
    if protected["on"]["push"]["branches"] != ["main"]:
        findings.append("protected admission is not limited to main")
    if ci.get("permissions") != {"contents": "read"}:
        findings.append("vNext CI has excessive top-level permissions")
    if protected.get("permissions") != {"contents": "read"}:
        findings.append(
            "protected workflow has excessive top-level permissions"
        )

    candidate = protected["jobs"]["candidate"]
    if candidate.get("permissions") != {"contents": "read"}:
        findings.append("candidate job has privileged permissions")
    attest = protected["jobs"].get(privileged_policy["job_id"])
    if attest is None:
        findings.append("privileged attestation job is missing")
        return findings
    if attest.get("environment") != privileged_policy["environment"]:
        findings.append("privileged job environment is not protected")
    if attest.get("permissions") != privileged_policy[
        "required_permissions"
    ]:
        findings.append("privileged job permissions are not exact")
    privileged_steps = attest.get("steps", [])
    forbidden_actions = set(privileged_policy["forbidden_actions"])
    for step in privileged_steps:
        action = step.get("uses", "").split("@", 1)[0]
        if action in forbidden_actions:
            findings.append(
                "privileged job uses forbidden action {}".format(action)
            )
    run_step_names = [
        step.get("name")
        for step in privileged_steps
        if "run" in step
    ]
    if run_step_names != privileged_policy["allowed_run_step_names"]:
        findings.append(
            "privileged job executes an unapproved run step"
        )
    if "secrets: inherit" in protected_path.read_text(encoding="utf-8"):
        findings.append("protected workflow inherits caller secrets")
    for workflow in (ci, protected):
        for step in _all_steps(workflow):
            action = step.get("uses", "")
            if action.startswith("actions/checkout@") and step.get(
                "with", {}
            ).get("persist-credentials") != "false":
                findings.append(
                    "checkout must disable persisted credentials"
                )
    return findings


def main() -> int:
    try:
        policy = _load_json_strict(POLICY_PATH)
        findings = verify_deployment(policy)
    except (OSError, ValueError, KeyError, TypeError) as error:
        findings = [str(error)]
    print(
        json.dumps(
            {
                "status": "passed" if not findings else "failed",
                "findings": findings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
