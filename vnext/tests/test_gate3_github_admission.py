from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


VNEXT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = VNEXT_ROOT.parent
sys.path.insert(0, str(VNEXT_ROOT / "tools"))

from build_gate3_github_admission_request import (  # noqa: E402
    _sha256_list_environment,
    build_request,
)
from gate3_admission_authority import (  # noqa: E402
    AdmissionExpectation,
)
from github_gate3_admission import (  # noqa: E402
    GITHUB_REPOSITORY,
    GITHUB_REPOSITORY_ID,
    GITHUB_REPOSITORY_OWNER_ID,
    TRUSTED_ENVIRONMENT,
    TRUSTED_SOURCE_REF,
    TRUSTED_WORKFLOW_PATH,
    build_gh_verify_command,
    admission_authority_sha256,
    validate_request_against_provider_state,
    verify_github_attestation,
)
from verify_gate3_e0 import compute_readiness  # noqa: E402
from verify_github_workflow_deployment import (  # noqa: E402
    verify_deployment,
)


REQUEST_SCHEMA_PATH = (
    VNEXT_ROOT
    / "evals"
    / "gate3"
    / "github-admission-request.schema.json"
)
STATE_SCHEMA_PATH = (
    VNEXT_ROOT
    / "evals"
    / "gate3"
    / "github-provider-state.schema.json"
)
PROTECTED_WORKFLOW_PATH = (
    WORKSPACE_ROOT
    / ".github"
    / "workflows"
    / "gate3-protected-admission.yml"
)
CI_WORKFLOW_PATH = (
    WORKSPACE_ROOT / ".github" / "workflows" / "vnext-ci.yml"
)
WORKFLOW_POLICY_PATH = (
    VNEXT_ROOT
    / "ops"
    / "github"
    / "workflow-authority-policy.json"
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Gate3GitHubProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.request_schema = _load_json(REQUEST_SCHEMA_PATH)
        self.state_schema = _load_json(STATE_SCHEMA_PATH)
        self.source_revision = "a" * 40
        self.request = {
            "artifact_type": "gate3_github_admission_request",
            "artifact_version": "gate3.github-admission-request.v1",
            "operation_id": "gate3-e0:100:2",
            "admission_authority_sha256": "0" * 64,
            "repository": {
                "name_with_owner": GITHUB_REPOSITORY,
                "repository_id": GITHUB_REPOSITORY_ID,
                "repository_owner_id": GITHUB_REPOSITORY_OWNER_ID,
                "source_revision": self.source_revision,
                "source_ref": TRUSTED_SOURCE_REF,
                "event_name": "push",
                "environment": TRUSTED_ENVIRONMENT,
            },
            "workflow": {
                "workflow_path": TRUSTED_WORKFLOW_PATH,
                "workflow_revision": self.source_revision,
                "run_id": 100,
                "run_attempt": 2,
                "runner_environment": "github-hosted",
            },
            "release_authority": {
                "release_epoch": 3,
                "trust_policy_epoch": 7,
                "policy_sha256": "1" * 64,
                "authority_root_bundle_sha256": "2" * 64,
                "verifier_release_sha256": "3" * 64,
                "evaluated_artifact_hashes": {
                    "gate3-eval-policy.json": "4" * 64,
                    "tools/verify_gate3_e0.py": "5" * 64,
                },
            },
            "runtime_attestation": {
                "python_version": "3.12.13",
                "python_executable_sha256": "6" * 64,
                "node_version": "v22.18.0",
                "node_executable_sha256": "7" * 64,
                "npm_version": "10.9.3",
                "npm_executable_sha256": "8" * 64,
                "uv_version": "uv 0.12.0",
                "uv_executable_sha256": "9" * 64,
                "dependency_inventory_sha256": "7" * 64,
                "import_inventory_sha256": "8" * 64,
                "source_tree_sha256": "9" * 64,
            },
            "authorization": {
                "authorized_attestation_sha256s": ["b" * 64],
                "authorized_manifest_sha256s": ["c" * 64],
            },
            "previous_admission_sha256": "d" * 64,
            "created_at": "2026-07-30T12:00:00Z",
        }
        self.request["admission_authority_sha256"] = (
            admission_authority_sha256(self.request)
        )
        self.state = {
            "artifact_type": "gate3_github_provider_state",
            "artifact_version": "gate3.github-provider-state.v1",
            "trust_domain": "waje-gate3-github-public",
            "repository_id": GITHUB_REPOSITORY_ID,
            "repository_owner_id": GITHUB_REPOSITORY_OWNER_ID,
            "trusted_source_ref": TRUSTED_SOURCE_REF,
            "trusted_environment": TRUSTED_ENVIRONMENT,
            "trusted_workflow_path": TRUSTED_WORKFLOW_PATH,
            "trusted_workflow_revision": self.source_revision,
            "current_release_epoch": 3,
            "approved_admission_authority_sha256": self.request[
                "admission_authority_sha256"
            ],
            "gh_executable_sha256": "e" * 64,
            "minimum_trust_policy_epoch": 7,
            "previous_admission_sha256": "d" * 64,
            "previous_provider_state_sha256": "f" * 64,
            "state_version": 11,
        }

    def _validate(
        self,
        request: dict | None = None,
        state: dict | None = None,
    ):
        return validate_request_against_provider_state(
            request or self.request,
            state or self.state,
            request_schema=self.request_schema,
            provider_state_schema=self.state_schema,
        )

    def test_exact_provider_state_is_accepted(self) -> None:
        provider_state, findings = self._validate()
        self.assertEqual([], findings)
        self.assertIsNotNone(provider_state)
        self.assertEqual(11, provider_state.state_version)
        self.assertEqual(
            "f" * 64,
            provider_state.previous_provider_state_sha256,
        )

    def test_identity_and_authority_drift_is_rejected(self) -> None:
        attacks = (
            ("repository", "repository_id", GITHUB_REPOSITORY_ID + 1),
            (
                "repository",
                "repository_owner_id",
                GITHUB_REPOSITORY_OWNER_ID + 1,
            ),
            ("repository", "source_ref", "refs/heads/lookalike"),
            ("repository", "event_name", "pull_request_target"),
            ("repository", "environment", "unprotected"),
            ("workflow", "workflow_revision", "e" * 40),
            ("workflow", "runner_environment", "self-hosted"),
            ("workflow", "run_attempt", 3),
            ("release_authority", "release_epoch", 2),
            ("release_authority", "trust_policy_epoch", 6),
        )
        for section, field, value in attacks:
            with self.subTest(field=field):
                request = copy.deepcopy(self.request)
                request[section][field] = value
                _, findings = self._validate(request=request)
                self.assertTrue(findings)

    def test_same_epoch_with_different_authority_hash_is_rejected(
        self,
    ) -> None:
        state = copy.deepcopy(self.state)
        state["approved_admission_authority_sha256"] = "f" * 64
        provider_state, findings = self._validate(state=state)
        self.assertIsNone(provider_state)
        self.assertTrue(
            any("admission_authority_sha256" in item for item in findings)
        )

    def test_runtime_and_authorization_are_part_of_external_approval(
        self,
    ) -> None:
        attacks = (
            ("runtime_attestation", "python_executable_sha256", "f" * 64),
            (
                "authorization",
                "authorized_attestation_sha256s",
                ["e" * 64],
            ),
            (
                "authorization",
                "authorized_manifest_sha256s",
                ["a" * 64],
            ),
        )
        for section, field, value in attacks:
            with self.subTest(section=section, field=field):
                request = copy.deepcopy(self.request)
                request[section][field] = value
                request["admission_authority_sha256"] = (
                    admission_authority_sha256(request)
                )
                provider_state, findings = self._validate(request=request)
                self.assertIsNone(provider_state)
                self.assertTrue(
                    any(
                        "admission_authority_sha256" in finding
                        for finding in findings
                    )
                )

    def test_previous_receipt_and_operation_are_monotonic(self) -> None:
        for field in ("operation_id", "previous_admission_sha256"):
            with self.subTest(field=field):
                request = copy.deepcopy(self.request)
                request[field] = (
                    "gate3-e0:other"
                    if field == "operation_id"
                    else "e" * 64
                )
                _, findings = self._validate(request=request)
                self.assertTrue(findings)

    def test_gh_command_enforces_full_provider_identity(self) -> None:
        command = build_gh_verify_command(
            gh_executable=Path("/opt/waje/bin/gh"),
            request_path=Path("/input/request.json"),
            bundle_path=Path("/input/bundle.jsonl"),
            source_revision=self.source_revision,
            workflow_revision=self.source_revision,
        )
        rendered = " ".join(command)
        for required in (
            "--repo {}".format(GITHUB_REPOSITORY),
            "--source-digest {}".format(self.source_revision),
            "--source-ref refs/heads/main",
            "--signer-digest {}".format(self.source_revision),
            "--deny-self-hosted-runners",
            "--cert-oidc-issuer https://token.actions.githubusercontent.com",
            "--predicate-type https://slsa.dev/provenance/v1",
        ):
            self.assertIn(required, rendered)
        self.assertNotIn("--owner ", rendered)

    def test_verified_bundle_returns_hash_bound_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            bundle_path = root / "bundle.jsonl"
            gh_path = root / "gh"
            request_path.write_text(
                json.dumps(self.request, sort_keys=True),
                encoding="utf-8",
            )
            bundle_path.write_text("{}\n", encoding="utf-8")
            gh_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            gh_path.chmod(
                gh_path.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
            request_hash = _sha256(request_path)
            self.state["gh_executable_sha256"] = _sha256(gh_path)
            result = [
                {
                    "attestation": {},
                    "verificationResult": {
                        "statement": {
                            "subject": [
                                {
                                    "name": request_path.name,
                                    "digest": {"sha256": request_hash},
                                }
                            ]
                        },
                        "signature": {
                            "certificate": {
                                "runnerEnvironment": "github-hosted"
                            }
                        },
                    },
                }
            ]
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(result),
                stderr="",
            )
            with patch(
                "github_gate3_admission.subprocess.run",
                return_value=completed,
            ) as run:
                verified, findings = verify_github_attestation(
                    request_path,
                    bundle_path,
                    self.state,
                    request_schema=self.request_schema,
                    provider_state_schema=self.state_schema,
                    gh_executable=gh_path,
                )
            self.assertEqual([], findings)
            self.assertIsNotNone(verified)
            self.assertEqual(request_hash, verified.request_sha256)
            self.assertEqual(11, verified.provider_state_version)
            self.assertEqual(
                "f" * 64,
                verified.previous_provider_state_sha256,
            )
            self.assertNotEqual(
                verified.bundle_sha256,
                verified.receipt_sha256,
            )
            self.assertEqual(
                frozenset({"b" * 64}),
                verified.authorized_attestation_sha256s,
            )
            release = self.request["release_authority"]
            expected = AdmissionExpectation(
                policy_sha256=release["policy_sha256"],
                authority_root_bundle_sha256=release[
                    "authority_root_bundle_sha256"
                ],
                verifier_release_sha256=release[
                    "verifier_release_sha256"
                ],
                evaluated_artifact_hashes=release[
                    "evaluated_artifact_hashes"
                ],
            )
            authority = verified.as_admission_authority(expected)
            self.assertEqual(
                verified.provider_state_sha256,
                authority.authority_state_sha256,
            )
            self.assertEqual(
                verified.previous_admission_sha256,
                authority.predecessor_receipt_sha256,
            )
            with self.assertRaisesRegex(
                ValueError,
                "does not match local expectation",
            ):
                verified.as_admission_authority(
                    AdmissionExpectation(
                        policy_sha256="0" * 64,
                        authority_root_bundle_sha256=(
                            expected.authority_root_bundle_sha256
                        ),
                        verifier_release_sha256=(
                            expected.verifier_release_sha256
                        ),
                        evaluated_artifact_hashes=(
                            expected.evaluated_artifact_hashes
                        ),
                    )
                )
            run.assert_called_once()
            self.assertFalse(run.call_args.kwargs["check"])
            self.assertNotIn("shell", run.call_args.kwargs)
            verifier_environment = run.call_args.kwargs["env"]
            self.assertNotIn("GH_TOKEN", verifier_environment)
            self.assertNotIn("LD_PRELOAD", verifier_environment)
            self.assertNotEqual(
                str(gh_path.parent),
                verifier_environment["PATH"],
            )
            command = run.call_args.args[0]
            self.assertTrue(command[0].endswith("/gh"))
            self.assertNotIn(str(request_path), command)
            self.assertNotIn(str(bundle_path), command)
            self.assertIn("--limit", command)
            self.assertIn("1", command)

    def test_non_github_hosted_certificate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            bundle_path = root / "bundle.jsonl"
            gh_path = root / "gh"
            request_path.write_text(
                json.dumps(self.request, sort_keys=True),
                encoding="utf-8",
            )
            bundle_path.write_text("{}\n", encoding="utf-8")
            gh_path.write_text("#!/bin/sh\n", encoding="utf-8")
            gh_path.chmod(0o755)
            request_hash = _sha256(request_path)
            self.state["gh_executable_sha256"] = _sha256(gh_path)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "verificationResult": {
                                "statement": {
                                    "subject": [
                                        {
                                            "digest": {
                                                "sha256": request_hash
                                            }
                                        }
                                    ]
                                },
                                "signature": {
                                    "certificate": {
                                        "runnerEnvironment": "self-hosted"
                                    }
                                },
                            }
                        }
                    ]
                ),
                stderr="",
            )
            with patch(
                "github_gate3_admission.subprocess.run",
                return_value=completed,
            ):
                verified, findings = verify_github_attestation(
                    request_path,
                    bundle_path,
                    self.state,
                    request_schema=self.request_schema,
                    provider_state_schema=self.state_schema,
                    gh_executable=gh_path,
                )
            self.assertIsNone(verified)
            self.assertIn(
                "GitHub Sigstore subject does not match admission request",
                findings,
            )

    def test_subject_substitution_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            bundle_path = root / "bundle.jsonl"
            gh_path = root / "gh"
            request_path.write_text(
                json.dumps(self.request, sort_keys=True),
                encoding="utf-8",
            )
            bundle_path.write_text("{}\n", encoding="utf-8")
            gh_path.write_text("#!/bin/sh\n", encoding="utf-8")
            gh_path.chmod(0o755)
            self.state["gh_executable_sha256"] = _sha256(gh_path)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "verificationResult": {
                                "statement": {
                                    "subject": [
                                        {"digest": {"sha256": "0" * 64}}
                                    ]
                                }
                            }
                        }
                    ]
                ),
                stderr="",
            )
            with patch(
                "github_gate3_admission.subprocess.run",
                return_value=completed,
            ):
                verified, findings = verify_github_attestation(
                    request_path,
                    bundle_path,
                    self.state,
                    request_schema=self.request_schema,
                    provider_state_schema=self.state_schema,
                    gh_executable=gh_path,
                )
            self.assertIsNone(verified)
            self.assertIn(
                "GitHub Sigstore subject does not match admission request",
                findings,
            )

    def test_duplicate_json_key_is_rejected_before_sigstore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            bundle_path = root / "bundle.jsonl"
            gh_path = root / "gh"
            request_path.write_text(
                '{"artifact_type":"a","artifact_type":"b"}',
                encoding="utf-8",
            )
            bundle_path.write_text("{}\n", encoding="utf-8")
            gh_path.write_text("#!/bin/sh\n", encoding="utf-8")
            gh_path.chmod(0o755)
            self.state["gh_executable_sha256"] = _sha256(gh_path)
            with patch(
                "github_gate3_admission.subprocess.run"
            ) as run:
                verified, findings = verify_github_attestation(
                    request_path,
                    bundle_path,
                    self.state,
                    request_schema=self.request_schema,
                    provider_state_schema=self.state_schema,
                    gh_executable=gh_path,
                )
            self.assertIsNone(verified)
            self.assertTrue(
                any("duplicate JSON key" in item for item in findings)
            )
            run.assert_not_called()

    def test_local_environment_cannot_unlock_canonical_gate(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY": GITHUB_REPOSITORY,
                "GATE3_APPROVED_ADMISSION_AUTHORITY_SHA256": (
                    self.state["approved_admission_authority_sha256"]
                ),
            },
            clear=False,
        ):
            readiness, _ = compute_readiness()
        condition = next(
            item
            for item in readiness["condition_verdicts"]
            if item["condition_id"] == "external_admission_verified"
        )
        self.assertEqual("blocked", condition["verdict"])
        with self.assertRaises(TypeError):
            compute_readiness(github_provider_state=self.state)

    def test_candidate_builder_rejects_forged_github_identity(self) -> None:
        environment = {
            "GITHUB_REPOSITORY": GITHUB_REPOSITORY,
            "GITHUB_REPOSITORY_ID": str(GITHUB_REPOSITORY_ID),
            "GITHUB_REPOSITORY_OWNER_ID": str(
                GITHUB_REPOSITORY_OWNER_ID
            ),
            "GITHUB_REF": TRUSTED_SOURCE_REF,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_WORKFLOW_REF": "{}/{}@{}".format(
                GITHUB_REPOSITORY,
                TRUSTED_WORKFLOW_PATH,
                TRUSTED_SOURCE_REF,
            ),
            "GITHUB_SHA": self.source_revision,
            "GITHUB_WORKFLOW_SHA": self.source_revision,
            "GITHUB_RUN_ID": "100",
            "GITHUB_RUN_ATTEMPT": "2",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "WAJE_GATE3_ENVIRONMENT": TRUSTED_ENVIRONMENT,
            "GATE3_RELEASE_EPOCH": "3",
            "GATE3_TRUST_POLICY_EPOCH": "7",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "build_gate3_github_admission_request._tool_attestation",
                side_effect=(
                    ("v22.18.0", "a" * 64),
                    ("10.9.3", "b" * 64),
                    ("uv 0.12.0", "c" * 64),
                ),
            ),
            patch(
                "build_gate3_github_admission_request._dependency_inventory",
                return_value=[],
            ),
            patch(
                "build_gate3_github_admission_request._import_inventory",
                return_value=[],
            ),
        ):
            request = build_request()
        self.assertEqual(
            admission_authority_sha256(request),
            request["admission_authority_sha256"],
        )
        attacked = dict(environment)
        attacked["GITHUB_REPOSITORY_ID"] = str(
            GITHUB_REPOSITORY_ID + 1
        )
        with patch.dict(os.environ, attacked, clear=True):
            with self.assertRaisesRegex(ValueError, "not trusted"):
                build_request()

    def test_candidate_builder_rejects_unpinned_runtime_tools(
        self,
    ) -> None:
        with patch(
            "build_gate3_github_admission_request._tool_attestation",
            side_effect=(
                ("v26.0.0", "a" * 64),
                ("10.9.3", "b" * 64),
                ("uv 0.12.0", "c" * 64),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Node v22.18.0"):
                from build_gate3_github_admission_request import (
                    _runtime_attestation,
                )

                _runtime_attestation({})

    def test_candidate_authorization_proposal_is_strict_and_sorted(
        self,
    ) -> None:
        variable = (
            "GATE3_CANDIDATE_AUTHORIZED_ATTESTATION_SHA256S_JSON"
        )
        with patch.dict(
            os.environ,
            {variable: json.dumps(["b" * 64, "a" * 64])},
            clear=True,
        ):
            self.assertEqual(
                ["a" * 64, "b" * 64],
                _sha256_list_environment(variable),
            )
        with patch.dict(
            os.environ,
            {variable: json.dumps(["a" * 64, "a" * 64])},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "duplicates"):
                _sha256_list_environment(variable)


class Gate3GitHubWorkflowSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = _load_json(WORKFLOW_POLICY_PATH)

    def _load_workflow(self, path: Path) -> dict:
        return yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )

    def _all_uses(self, workflow: dict) -> list[str]:
        return [
            step["uses"]
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "uses" in step
        ]

    def test_all_actions_are_pinned_to_full_commit(self) -> None:
        pattern = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[a-f0-9]{40}$")
        for action, revision in self.policy["approved_actions"].items():
            self.assertRegex("{}@{}".format(action, revision), pattern)
        for path in (CI_WORKFLOW_PATH, PROTECTED_WORKFLOW_PATH):
            with self.subTest(path=path.name):
                if not path.exists():
                    continue
                workflow = self._load_workflow(path)
                for action in self._all_uses(workflow):
                    self.assertRegex(action, pattern)

    def test_untrusted_events_have_no_admission_permission(self) -> None:
        self.assertEqual(
            ["push"],
            self.policy["protected_events"],
        )
        self.assertEqual(
            {
                "pull_request_target",
                "workflow_dispatch",
                "workflow_run",
            },
            set(self.policy["forbidden_events"]),
        )
        if not CI_WORKFLOW_PATH.exists():
            return
        ci = self._load_workflow(CI_WORKFLOW_PATH)
        self.assertEqual({"contents": "read"}, ci["permissions"])
        self.assertNotIn("pull_request_target", ci["on"])
        self.assertNotIn("workflow_run", ci["on"])

        protected = self._load_workflow(PROTECTED_WORKFLOW_PATH)
        self.assertEqual({"push"}, set(protected["on"]))
        self.assertEqual(["main"], protected["on"]["push"]["branches"])
        candidate_permissions = protected["jobs"]["candidate"][
            "permissions"
        ]
        self.assertEqual({"contents": "read"}, candidate_permissions)
        attest_permissions = protected["jobs"]["attest"]["permissions"]
        self.assertEqual("write", attest_permissions["id-token"])
        self.assertEqual("write", attest_permissions["attestations"])
        self.assertEqual(
            "gate3-admission",
            protected["jobs"]["attest"]["environment"],
        )

    def test_privileged_job_does_not_checkout_or_execute_repo_code(self) -> None:
        privileged_policy = self.policy["privileged_job"]
        self.assertIn(
            "actions/checkout",
            privileged_policy["forbidden_actions"],
        )
        self.assertEqual(
            ["Enforce protected provider state"],
            privileged_policy["allowed_run_step_names"],
        )
        if not PROTECTED_WORKFLOW_PATH.exists():
            return
        protected = self._load_workflow(PROTECTED_WORKFLOW_PATH)
        steps = protected["jobs"]["attest"]["steps"]
        uses = [step.get("uses", "") for step in steps]
        self.assertFalse(
            any(action.startswith("actions/checkout@") for action in uses)
        )
        run_steps = [step for step in steps if "run" in step]
        self.assertEqual(
            ["Enforce protected provider state"],
            [step["name"] for step in run_steps],
        )
        self.assertNotIn("secrets: inherit", PROTECTED_WORKFLOW_PATH.read_text())

    def test_privileged_job_binds_runtime_and_authorization_to_approval(
        self,
    ) -> None:
        workflow_text = PROTECTED_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn(
            '"runtime_attestation": runtime',
            workflow_text,
        )
        self.assertIn(
            '"authorization": {',
            workflow_text,
        )
        self.assertIn(
            "APPROVED_ADMISSION_AUTHORITY_SHA256",
            workflow_text,
        )

    def test_parent_relative_run_fragment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for source in (
                WORKSPACE_ROOT / ".github" / "CODEOWNERS",
                CI_WORKFLOW_PATH,
                PROTECTED_WORKFLOW_PATH,
            ):
                relative = source.relative_to(WORKSPACE_ROOT)
                destination = workspace / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            protected = (
                workspace
                / ".github"
                / "workflows"
                / "gate3-protected-admission.yml"
            )
            content = protected.read_text(encoding="utf-8")
            content = content.replace(
                "      - name: Build admission request\n",
                "      - name: Escape isolated root\n"
                "        run: ../outside/run.py\n\n"
                "      - name: Build admission request\n",
            )
            protected.write_text(content, encoding="utf-8")
            policy = copy.deepcopy(self.policy)
            for reference in policy["deployment_files"]:
                policy["deployment_files"][reference] = _sha256(
                    workspace / reference
                )
            findings = verify_deployment(
                policy,
                workspace_root=workspace,
            )
            self.assertTrue(
                any("escapes the vnext boundary" in item for item in findings)
            )

    def test_extra_unprotected_signer_job_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for source in (
                WORKSPACE_ROOT / ".github" / "CODEOWNERS",
                CI_WORKFLOW_PATH,
                PROTECTED_WORKFLOW_PATH,
            ):
                relative = source.relative_to(WORKSPACE_ROOT)
                destination = workspace / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            protected = (
                workspace
                / ".github"
                / "workflows"
                / "gate3-protected-admission.yml"
            )
            content = protected.read_text(encoding="utf-8")
            content += (
                "\n  unprotected-signer:\n"
                "    runs-on: ubuntu-24.04\n"
                "    permissions:\n"
                "      contents: read\n"
                "      id-token: write\n"
                "      attestations: write\n"
                "    steps:\n"
                "      - uses: "
                "actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d\n"
                "        with:\n"
                "          subject-path: forged.json\n"
            )
            protected.write_text(content, encoding="utf-8")
            policy = copy.deepcopy(self.policy)
            for reference in policy["deployment_files"]:
                policy["deployment_files"][reference] = _sha256(
                    workspace / reference
                )
            findings = verify_deployment(
                policy,
                workspace_root=workspace,
            )
            self.assertTrue(
                any("job set does not match" in item for item in findings)
            )
            self.assertTrue(
                any("privileged permissions" in item for item in findings)
            )
            self.assertTrue(
                any("attestation signer" in item for item in findings)
            )

    def test_deployment_projection_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for source in (
                WORKSPACE_ROOT / ".github" / "CODEOWNERS",
                CI_WORKFLOW_PATH,
                PROTECTED_WORKFLOW_PATH,
            ):
                relative = source.relative_to(WORKSPACE_ROOT)
                destination = workspace / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            ci = workspace / ".github" / "workflows" / "vnext-ci.yml"
            target = ci.with_name("vnext-ci-target.yml")
            ci.rename(target)
            ci.symlink_to(target.name)
            policy = copy.deepcopy(self.policy)
            for reference in policy["deployment_files"]:
                policy["deployment_files"][reference] = _sha256(
                    workspace / reference
                )
            findings = verify_deployment(
                policy,
                workspace_root=workspace,
            )
            self.assertTrue(
                any("missing or unsafe" in item for item in findings)
            )


if __name__ == "__main__":
    unittest.main()
