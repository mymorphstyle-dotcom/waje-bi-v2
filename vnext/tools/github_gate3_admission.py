#!/usr/bin/env python3
"""GitHub Actions/Sigstore provider contract for Gate 3 admission.

This module keeps GitHub-specific trust outside the business evaluator. The
provider state must come from the protected control plane. Repository files and
``GITHUB_*`` environment variables cannot supply that state.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from gate3_admission_authority import (
    AdmissionExpectation,
    VerifiedAdmissionAuthority,
    canonical_sha256,
)


GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_HOST = "github.com"
GITHUB_REPOSITORY = "mymorphstyle-dotcom/waje-bi-v2"
GITHUB_REPOSITORY_ID = 1317104320
GITHUB_REPOSITORY_OWNER_ID = 278493004
TRUSTED_SOURCE_REF = "refs/heads/main"
TRUSTED_ENVIRONMENT = "gate3-admission"
TRUSTED_EVENT = "push"
TRUSTED_WORKFLOW_PATH = (
    ".github/workflows/gate3-protected-admission.yml"
)
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_TRUSTED_ROOT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class GitHubProviderState:
    """Monotonic state supplied by the protected GitHub control plane."""

    trusted_workflow_revision: str
    current_release_epoch: int
    approved_admission_authority_sha256: str
    gh_executable_sha256: str
    trusted_root_sha256: str
    minimum_trust_policy_epoch: int
    previous_admission_sha256: str | None
    previous_provider_state_sha256: str | None
    state_version: int
    valid_from: datetime
    valid_until: datetime
    maximum_request_age: timedelta
    maximum_attestation_delay: timedelta
    clock_skew: timedelta


@dataclass(frozen=True)
class VerifiedGitHubAttestation:
    """Immutable result after Sigstore and provider-policy verification."""

    request_sha256: str
    bundle_sha256: str
    receipt_sha256: str
    provider_state_sha256: str
    provider_state_version: int
    previous_admission_sha256: str | None
    previous_provider_state_sha256: str | None
    repository_id: int
    repository_owner_id: int
    source_revision: str
    source_ref: str
    workflow_revision: str
    run_id: int
    run_attempt: int
    admission_authority_sha256: str
    release_epoch: int
    policy_sha256: str
    authority_root_bundle_sha256: str
    verifier_release_sha256: str
    evaluated_artifact_hashes: Mapping[str, str]
    authorized_attestation_sha256s: frozenset[str]
    authorized_manifest_sha256s: frozenset[str]
    verification_result: Mapping[str, Any]
    verified_timestamp: datetime

    def as_admission_authority(
        self,
        expected: AdmissionExpectation,
    ) -> VerifiedAdmissionAuthority:
        attested = AdmissionExpectation(
            policy_sha256=self.policy_sha256,
            authority_root_bundle_sha256=(
                self.authority_root_bundle_sha256
            ),
            verifier_release_sha256=self.verifier_release_sha256,
            evaluated_artifact_hashes=self.evaluated_artifact_hashes,
        )
        if (
            attested.policy_sha256 != expected.policy_sha256
            or attested.authority_root_bundle_sha256
            != expected.authority_root_bundle_sha256
            or attested.verifier_release_sha256
            != expected.verifier_release_sha256
            or dict(attested.evaluated_artifact_hashes)
            != dict(expected.evaluated_artifact_hashes)
        ):
            raise ValueError(
                "verified GitHub admission does not match local expectation"
            )
        return VerifiedAdmissionAuthority.create(
            issuer_id="github-actions-sigstore",
            authority_key_id=self.workflow_revision,
            receipt_sha256=self.receipt_sha256,
            authority_state_sha256=self.provider_state_sha256,
            authority_state_version=self.provider_state_version,
            predecessor_receipt_sha256=self.previous_admission_sha256,
            expectation=expected,
            authorized_attestation_sha256s=(
                self.authorized_attestation_sha256s
            ),
            authorized_manifest_sha256s=(
                self.authorized_manifest_sha256s
            ),
        )


@dataclass(frozen=True)
class AdmissionStateHead:
    """Latest externally committed admission transition."""

    provider_state_sha256: str
    provider_state_version: int
    admission_receipt_sha256: str
    workflow_revision: str
    run_id: int
    run_attempt: int
    verified_at: datetime
    valid_until: datetime
    clock_skew: timedelta
    authority: VerifiedAdmissionAuthority


@dataclass(frozen=True)
class AdmissionControlSnapshot:
    """Atomically observed provider state and committed admission head."""

    provider_state: Mapping[str, Any]
    provider_state_revision: str
    admission_head: AdmissionStateHead | None


class GitHubAdmissionControlPlane(Protocol):
    """Atomic provider-state and admission-head storage boundary."""

    def read_snapshot(self) -> AdmissionControlSnapshot:
        """Atomically read provider state and admission head."""

    def compare_and_swap(
        self,
        expected: AdmissionControlSnapshot,
        replacement: AdmissionStateHead,
    ) -> bool:
        """Commit iff provider state and admission head still match."""


class InMemoryAdmissionStateCAS:
    """Process-local CAS for tests and composition probes.

    It is intentionally not a durable production adapter. The canonical
    deployment must provide the same interface from an external transactional
    store.
    """

    def __init__(self, provider_state: Mapping[str, Any]) -> None:
        self._provider_state = dict(provider_state)
        self._provider_state_revision = canonical_sha256(provider_state)
        self._head: AdmissionStateHead | None = None
        self._lock = threading.Lock()

    def read_snapshot(self) -> AdmissionControlSnapshot:
        with self._lock:
            return AdmissionControlSnapshot(
                provider_state=MappingProxyType(
                    dict(self._provider_state)
                ),
                provider_state_revision=self._provider_state_revision,
                admission_head=self._head,
            )

    def compare_and_swap(
        self,
        expected: AdmissionControlSnapshot,
        replacement: AdmissionStateHead,
    ) -> bool:
        with self._lock:
            if (
                self._provider_state_revision
                != expected.provider_state_revision
                or self._head != expected.admission_head
            ):
                return False
            self._head = replacement
            return True

    def replace_provider_state(
        self,
        provider_state: Mapping[str, Any],
    ) -> None:
        """Test/probe helper that atomically advances provider-owned state."""

        with self._lock:
            self._provider_state = dict(provider_state)
            self._provider_state_revision = canonical_sha256(provider_state)


def _schema_findings(
    value: Any,
    schema: Mapping[str, Any],
    *,
    label: str,
) -> list[str]:
    return [
        "{} {}: {}".format(
            label,
            "/".join(str(part) for part in error.absolute_path)
            or "<root>",
            error.message,
        )
        for error in Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value)
    ]


def admission_authority_payload(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    release = request["release_authority"]
    return {
        "release_authority": {
            "release_epoch": release["release_epoch"],
            "trust_policy_epoch": release["trust_policy_epoch"],
            "policy_sha256": release["policy_sha256"],
            "authority_root_bundle_sha256": release[
                "authority_root_bundle_sha256"
            ],
            "verifier_release_sha256": release[
                "verifier_release_sha256"
            ],
            "evaluated_artifact_hashes": dict(
                release["evaluated_artifact_hashes"]
            ),
        },
        "runtime_attestation": dict(request["runtime_attestation"]),
        "authorization": {
            "authorized_attestation_sha256s": sorted(
                request["authorization"][
                    "authorized_attestation_sha256s"
                ]
            ),
            "authorized_manifest_sha256s": sorted(
                request["authorization"]["authorized_manifest_sha256s"]
            ),
        },
    }


def admission_authority_sha256(request: Mapping[str, Any]) -> str:
    """Bind release, runtime and authorization into one approved identity."""

    return canonical_sha256(admission_authority_payload(request))


def _parse_datetime(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("{} is not an RFC3339 timestamp".format(label)) from error
    if parsed.tzinfo is None:
        raise ValueError("{} must include a timezone".format(label))
    return parsed.astimezone(timezone.utc)


def validate_request_against_provider_state(
    request: Mapping[str, Any],
    provider_state_document: Mapping[str, Any],
    *,
    request_schema: Mapping[str, Any],
    provider_state_schema: Mapping[str, Any],
    verification_time: datetime,
) -> tuple[GitHubProviderState | None, list[str]]:
    """Validate candidate facts against externally controlled state."""

    findings = _schema_findings(
        request,
        request_schema,
        label="GitHub admission request",
    )
    findings.extend(
        _schema_findings(
            provider_state_document,
            provider_state_schema,
            label="GitHub provider state",
        )
    )
    if findings:
        return None, findings

    if verification_time.tzinfo is None:
        return None, ["verification time must include a timezone"]
    verification_time = verification_time.astimezone(timezone.utc)
    repository = request["repository"]
    workflow = request["workflow"]
    release = request["release_authority"]
    try:
        valid_from = _parse_datetime(
            provider_state_document["valid_from"],
            label="provider state valid_from",
        )
        valid_until = _parse_datetime(
            provider_state_document["valid_until"],
            label="provider state valid_until",
        )
        request_created_at = _parse_datetime(
            request["created_at"],
            label="admission request created_at",
        )
    except ValueError as error:
        return None, [str(error)]
    state = GitHubProviderState(
        trusted_workflow_revision=provider_state_document[
            "trusted_workflow_revision"
        ],
        current_release_epoch=provider_state_document[
            "current_release_epoch"
        ],
        approved_admission_authority_sha256=provider_state_document[
            "approved_admission_authority_sha256"
        ],
        gh_executable_sha256=provider_state_document[
            "gh_executable_sha256"
        ],
        trusted_root_sha256=provider_state_document[
            "trusted_root_sha256"
        ],
        minimum_trust_policy_epoch=provider_state_document[
            "minimum_trust_policy_epoch"
        ],
        previous_admission_sha256=provider_state_document[
            "previous_admission_sha256"
        ],
        previous_provider_state_sha256=provider_state_document[
            "previous_provider_state_sha256"
        ],
        state_version=provider_state_document["state_version"],
        valid_from=valid_from,
        valid_until=valid_until,
        maximum_request_age=timedelta(
            seconds=provider_state_document["maximum_request_age_seconds"]
        ),
        maximum_attestation_delay=timedelta(
            seconds=provider_state_document[
                "maximum_attestation_delay_seconds"
            ]
        ),
        clock_skew=timedelta(
            seconds=provider_state_document["clock_skew_seconds"]
        ),
    )
    exact_matches = {
        "repository.name_with_owner": (
            repository["name_with_owner"],
            GITHUB_REPOSITORY,
        ),
        "repository.repository_id": (
            repository["repository_id"],
            GITHUB_REPOSITORY_ID,
        ),
        "repository.repository_owner_id": (
            repository["repository_owner_id"],
            GITHUB_REPOSITORY_OWNER_ID,
        ),
        "repository.source_ref": (
            repository["source_ref"],
            TRUSTED_SOURCE_REF,
        ),
        "repository.event_name": (
            repository["event_name"],
            TRUSTED_EVENT,
        ),
        "repository.environment": (
            repository["environment"],
            TRUSTED_ENVIRONMENT,
        ),
        "workflow.workflow_path": (
            workflow["workflow_path"],
            TRUSTED_WORKFLOW_PATH,
        ),
        "workflow.workflow_revision": (
            workflow["workflow_revision"],
            state.trusted_workflow_revision,
        ),
        "workflow.runner_environment": (
            workflow["runner_environment"],
            "github-hosted",
        ),
        "release.release_epoch": (
            release["release_epoch"],
            state.current_release_epoch,
        ),
        "admission_authority_sha256": (
            request["admission_authority_sha256"],
            state.approved_admission_authority_sha256,
        ),
        "previous_admission_sha256": (
            request["previous_admission_sha256"],
            state.previous_admission_sha256,
        ),
    }
    for field, (actual, expected) in exact_matches.items():
        if actual != expected:
            findings.append(
                "{} does not match protected provider state".format(field)
            )

    expected_operation_id = "gate3-e0:{}:{}".format(
        workflow["run_id"],
        workflow["run_attempt"],
    )
    if request["operation_id"] != expected_operation_id:
        findings.append(
            "operation_id does not match GitHub run identity"
        )
    if workflow["workflow_revision"] != repository["source_revision"]:
        findings.append(
            "workflow revision does not match source revision"
        )
    computed_authority_hash = admission_authority_sha256(request)
    if request["admission_authority_sha256"] != computed_authority_hash:
        findings.append("admission authority hash is stale")
    if (
        release["trust_policy_epoch"]
        < state.minimum_trust_policy_epoch
    ):
        findings.append("trust policy epoch has rolled back")
    if state.valid_from >= state.valid_until:
        findings.append("provider state validity window is empty")
    if not (
        state.valid_from - state.clock_skew
        <= verification_time
        <= state.valid_until + state.clock_skew
    ):
        findings.append("provider state is not current at verification time")
    if not (
        state.valid_from - state.clock_skew
        <= request_created_at
        <= state.valid_until + state.clock_skew
    ):
        findings.append("admission request is outside provider validity window")
    if request_created_at > verification_time + state.clock_skew:
        findings.append("admission request was created in the future")
    if verification_time - request_created_at > state.maximum_request_age:
        findings.append("admission request is stale")

    return (None, findings) if findings else (state, [])


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("{} must be a regular file".format(label))
        if metadata.st_size > maximum_bytes:
            raise ValueError("{} exceeds size limit".format(label))
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = handle.read(maximum_bytes + 1)
        if len(value) > maximum_bytes:
            raise ValueError("{} exceeds size limit".format(label))
        return value
    finally:
        os.close(descriptor)


def _load_json_strict_bytes(value_bytes: bytes) -> Any:
    def reject_duplicates(
        pairs: Sequence[tuple[str, Any]],
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
        value_bytes.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
    )


def validate_gh_executable(
    gh_executable: Path,
    *,
    expected_sha256: str,
) -> list[str]:
    findings: list[str] = []
    if not gh_executable.is_absolute():
        return ["GitHub CLI path must be absolute"]
    try:
        metadata = gh_executable.lstat()
    except OSError:
        return ["GitHub CLI executable is unavailable"]
    if stat.S_ISLNK(metadata.st_mode):
        findings.append("GitHub CLI executable cannot be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        findings.append("GitHub CLI executable must be a regular file")
    if not os.access(gh_executable, os.X_OK):
        findings.append("GitHub CLI executable is not executable")
    if not findings and _file_sha256(gh_executable) != expected_sha256:
        findings.append("GitHub CLI executable digest is not approved")
    return findings


def build_gh_verify_command(
    *,
    gh_executable: Path,
    request_path: Path,
    bundle_path: Path,
    trusted_root_path: Path,
    source_revision: str,
    workflow_revision: str,
) -> tuple[str, ...]:
    signer_workflow = "{}/{}".format(
        GITHUB_REPOSITORY,
        TRUSTED_WORKFLOW_PATH,
    )
    return (
        str(gh_executable),
        "attestation",
        "verify",
        str(request_path),
        "--hostname",
        GITHUB_HOST,
        "--repo",
        GITHUB_REPOSITORY,
        "--bundle",
        str(bundle_path),
        "--custom-trusted-root",
        str(trusted_root_path),
        "--limit",
        "1",
        "--predicate-type",
        SLSA_PROVENANCE_V1,
        "--cert-oidc-issuer",
        GITHUB_OIDC_ISSUER,
        "--signer-workflow",
        signer_workflow,
        "--signer-digest",
        workflow_revision,
        "--source-digest",
        source_revision,
        "--source-ref",
        TRUSTED_SOURCE_REF,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    )


def _matching_verification_result(
    results: Sequence[Mapping[str, Any]],
    *,
    expected_sha256: str,
) -> Mapping[str, Any] | None:
    for result in results:
        verification = result.get("verificationResult")
        if not isinstance(verification, Mapping):
            continue
        signature = verification.get("signature")
        if not isinstance(signature, Mapping):
            continue
        certificate = signature.get("certificate")
        if (
            not isinstance(certificate, Mapping)
            or certificate.get("runnerEnvironment") != "github-hosted"
        ):
            continue
        statement = verification.get("statement")
        if not isinstance(statement, Mapping):
            continue
        subjects = statement.get("subject")
        if not isinstance(subjects, Sequence):
            continue
        for subject in subjects:
            if not isinstance(subject, Mapping):
                continue
            digest = subject.get("digest")
            if (
                isinstance(digest, Mapping)
                and digest.get("sha256") == expected_sha256
            ):
                return verification
    return None


def _verified_timestamp(
    verification: Mapping[str, Any],
    *,
    request_created_at: datetime,
    verification_time: datetime,
    state: GitHubProviderState,
) -> tuple[datetime | None, list[str]]:
    timestamps = verification.get("verifiedTimestamps")
    if not isinstance(timestamps, Sequence) or isinstance(
        timestamps, (str, bytes)
    ):
        return None, ["GitHub Sigstore verifier returned no trusted timestamp"]
    valid: list[datetime] = []
    malformed = False
    for timestamp in timestamps:
        if not isinstance(timestamp, Mapping):
            malformed = True
            continue
        value = timestamp.get("timestamp")
        if not isinstance(value, str):
            malformed = True
            continue
        try:
            parsed = _parse_datetime(
                value,
                label="verified Sigstore timestamp",
            )
        except ValueError:
            malformed = True
            continue
        if (
            state.valid_from - state.clock_skew
            <= parsed
            <= state.valid_until + state.clock_skew
            and request_created_at - state.clock_skew
            <= parsed
            <= request_created_at
            + state.maximum_attestation_delay
            + state.clock_skew
            and parsed
            <= verification_time + state.clock_skew
            and verification_time - parsed
            <= state.maximum_request_age
        ):
            valid.append(parsed)
    if malformed:
        return None, ["GitHub Sigstore verifier returned malformed timestamps"]
    if not valid:
        return None, [
            "GitHub Sigstore timestamp is outside the approved freshness window"
        ]
    return max(valid), []


def verify_github_attestation(
    request_path: Path,
    bundle_path: Path,
    provider_state_document: Mapping[str, Any],
    *,
    request_schema: Mapping[str, Any],
    provider_state_schema: Mapping[str, Any],
    gh_executable: Path,
    trusted_root_path: Path,
    verification_time: datetime,
) -> tuple[VerifiedGitHubAttestation | None, list[str]]:
    """Verify a GitHub/Sigstore bundle and its protected provider state."""

    try:
        request_bytes = _read_regular_file(
            request_path,
            maximum_bytes=MAX_REQUEST_BYTES,
            label="GitHub admission request",
        )
        bundle_bytes = _read_regular_file(
            bundle_path,
            maximum_bytes=MAX_BUNDLE_BYTES,
            label="Sigstore bundle",
        )
        trusted_root_bytes = _read_regular_file(
            trusted_root_path,
            maximum_bytes=MAX_TRUSTED_ROOT_BYTES,
            label="Sigstore trusted root",
        )
        request = _load_json_strict_bytes(request_bytes)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        return None, ["GitHub admission request is unreadable: {}".format(error)]
    state, findings = validate_request_against_provider_state(
        request,
        provider_state_document,
        request_schema=request_schema,
        provider_state_schema=provider_state_schema,
        verification_time=verification_time,
    )
    if state is None:
        return None, findings
    findings.extend(
        validate_gh_executable(
            gh_executable,
            expected_sha256=state.gh_executable_sha256,
        )
    )
    if findings:
        return None, findings
    trusted_root_hash = hashlib.sha256(trusted_root_bytes).hexdigest()
    if trusted_root_hash != state.trusted_root_sha256:
        return None, ["Sigstore trusted root digest is not approved"]

    request_hash = hashlib.sha256(request_bytes).hexdigest()
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    provider_state_hash = canonical_sha256(provider_state_document)
    receipt_hash = canonical_sha256(
        {
            "request_sha256": request_hash,
            "bundle_sha256": bundle_hash,
            "provider_state_sha256": provider_state_hash,
            "provider_state_version": state.state_version,
            "previous_admission_sha256": (
                state.previous_admission_sha256
            ),
            "previous_provider_state_sha256": (
                state.previous_provider_state_sha256
            ),
        }
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="waje-gate3-gh-verify-"
        ) as isolated_home:
            trusted_gh = Path(isolated_home) / "gh"
            trusted_request = Path(isolated_home) / "request.json"
            trusted_bundle = Path(isolated_home) / "bundle.jsonl"
            trusted_root = Path(isolated_home) / "trusted-root.jsonl"
            trusted_gh.write_bytes(gh_executable.read_bytes())
            trusted_gh.chmod(0o500)
            trusted_request.write_bytes(request_bytes)
            trusted_request.chmod(0o400)
            trusted_bundle.write_bytes(bundle_bytes)
            trusted_bundle.chmod(0o400)
            trusted_root.write_bytes(trusted_root_bytes)
            trusted_root.chmod(0o400)
            if _file_sha256(trusted_gh) != state.gh_executable_sha256:
                return None, [
                    "GitHub CLI changed while creating verifier snapshot"
                ]
            if (
                _file_sha256(trusted_request) != request_hash
                or _file_sha256(trusted_bundle) != bundle_hash
                or _file_sha256(trusted_root) != trusted_root_hash
            ):
                return None, [
                    "GitHub admission snapshot creation failed"
                ]
            command = build_gh_verify_command(
                gh_executable=trusted_gh,
                request_path=trusted_request,
                bundle_path=trusted_bundle,
                trusted_root_path=trusted_root,
                source_revision=request["repository"][
                    "source_revision"
                ],
                workflow_revision=state.trusted_workflow_revision,
            )
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env={
                    "GH_CONFIG_DIR": isolated_home,
                    "HOME": isolated_home,
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": isolated_home,
                    "XDG_CACHE_HOME": isolated_home,
                },
            )
            if (
                _file_sha256(trusted_request) != request_hash
                or _file_sha256(trusted_bundle) != bundle_hash
                or _file_sha256(trusted_root) != trusted_root_hash
            ):
                return None, [
                    "GitHub admission snapshots changed during verification"
                ]
    except (OSError, subprocess.TimeoutExpired):
        return None, ["GitHub Sigstore verifier is unavailable"]
    if completed.returncode != 0:
        return None, [
            "GitHub Sigstore verification failed with exit code {}".format(
                completed.returncode
            )
        ]
    try:
        results = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, ["GitHub Sigstore verifier returned invalid JSON"]
    if not isinstance(results, list) or not results:
        return None, ["GitHub Sigstore verifier returned no attestations"]
    matching_result = _matching_verification_result(
        results,
        expected_sha256=request_hash,
    )
    if matching_result is None:
        return None, [
            "GitHub Sigstore subject does not match admission request"
        ]
    request_created_at = _parse_datetime(
        request["created_at"],
        label="admission request created_at",
    )
    verified_timestamp, timestamp_findings = _verified_timestamp(
        matching_result,
        request_created_at=request_created_at,
        verification_time=verification_time.astimezone(timezone.utc),
        state=state,
    )
    if verified_timestamp is None:
        return None, timestamp_findings

    workflow = request["workflow"]
    release = request["release_authority"]
    authorization = request["authorization"]
    return (
        VerifiedGitHubAttestation(
            request_sha256=request_hash,
            bundle_sha256=bundle_hash,
            receipt_sha256=receipt_hash,
            provider_state_sha256=provider_state_hash,
            provider_state_version=state.state_version,
            previous_admission_sha256=state.previous_admission_sha256,
            previous_provider_state_sha256=(
                state.previous_provider_state_sha256
            ),
            repository_id=request["repository"]["repository_id"],
            repository_owner_id=request["repository"][
                "repository_owner_id"
            ],
            source_revision=request["repository"]["source_revision"],
            source_ref=request["repository"]["source_ref"],
            workflow_revision=workflow["workflow_revision"],
            run_id=workflow["run_id"],
            run_attempt=workflow["run_attempt"],
            admission_authority_sha256=request[
                "admission_authority_sha256"
            ],
            release_epoch=release["release_epoch"],
            policy_sha256=release["policy_sha256"],
            authority_root_bundle_sha256=release[
                "authority_root_bundle_sha256"
            ],
            verifier_release_sha256=release[
                "verifier_release_sha256"
            ],
            evaluated_artifact_hashes=MappingProxyType(
                dict(release["evaluated_artifact_hashes"])
            ),
            authorized_attestation_sha256s=frozenset(
                authorization["authorized_attestation_sha256s"]
            ),
            authorized_manifest_sha256s=frozenset(
                authorization["authorized_manifest_sha256s"]
            ),
            verification_result=MappingProxyType(
                dict(matching_result)
            ),
            verified_timestamp=verified_timestamp,
        ),
        [],
    )


def _monotonic_transition_findings(
    provider_state: Mapping[str, Any],
    current: AdmissionStateHead | None,
) -> list[str]:
    if current is None:
        findings: list[str] = []
        if provider_state["state_version"] != 1:
            findings.append("initial provider state version must be 1")
        if provider_state["previous_provider_state_sha256"] is not None:
            findings.append("initial provider state predecessor must be null")
        if provider_state["previous_admission_sha256"] is not None:
            findings.append("initial admission predecessor must be null")
        return findings
    findings = []
    if provider_state["state_version"] != current.provider_state_version + 1:
        findings.append("provider state version is not the next monotonic value")
    if (
        provider_state["previous_provider_state_sha256"]
        != current.provider_state_sha256
    ):
        findings.append("provider state predecessor does not match CAS head")
    if (
        provider_state["previous_admission_sha256"]
        != current.admission_receipt_sha256
    ):
        findings.append("admission predecessor does not match CAS head")
    return findings


class CanonicalGitHubAdmissionConnector:
    """Verify, atomically commit, and expose GitHub admission authority.

    The connector accepts provider state only through a protected source
    implementation and publishes authority only after cryptographic
    verification plus an atomic predecessor transition. The command-line
    evaluator receives this connector from trusted process composition; raw
    provider JSON and bundle paths are never readiness parameters.
    """

    def __init__(
        self,
        *,
        control_plane: GitHubAdmissionControlPlane,
        request_schema: Mapping[str, Any],
        provider_state_schema: Mapping[str, Any],
        gh_executable: Path,
        trusted_root_path: Path,
        verification_clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self._control_plane = control_plane
        self._request_schema = request_schema
        self._provider_state_schema = provider_state_schema
        self._gh_executable = gh_executable
        self._trusted_root_path = trusted_root_path
        self._verification_clock = verification_clock

    def admit(
        self,
        request_path: Path,
        bundle_path: Path,
        expected: AdmissionExpectation,
    ) -> tuple[VerifiedAdmissionAuthority | None, list[str]]:
        try:
            snapshot = self._control_plane.read_snapshot()
            provider_state = dict(snapshot.provider_state)
        except Exception as error:
            return None, [
                "protected provider state is unavailable: {}".format(error)
            ]
        state_findings = _schema_findings(
            provider_state,
            self._provider_state_schema,
            label="GitHub provider state",
        )
        if state_findings:
            return None, state_findings
        current = snapshot.admission_head
        transition_findings = _monotonic_transition_findings(
            provider_state,
            current,
        )
        if transition_findings:
            return None, transition_findings
        verification_time = self._verification_clock()
        verified, findings = verify_github_attestation(
            request_path,
            bundle_path,
            provider_state,
            request_schema=self._request_schema,
            provider_state_schema=self._provider_state_schema,
            gh_executable=self._gh_executable,
            trusted_root_path=self._trusted_root_path,
            verification_time=verification_time,
        )
        if verified is None:
            return None, findings
        try:
            authority = verified.as_admission_authority(expected)
            valid_until = _parse_datetime(
                provider_state["valid_until"],
                label="provider state valid_until",
            )
        except ValueError as error:
            return None, [str(error)]
        replacement = AdmissionStateHead(
            provider_state_sha256=verified.provider_state_sha256,
            provider_state_version=verified.provider_state_version,
            admission_receipt_sha256=verified.receipt_sha256,
            workflow_revision=verified.workflow_revision,
            run_id=verified.run_id,
            run_attempt=verified.run_attempt,
            verified_at=verification_time.astimezone(timezone.utc),
            valid_until=valid_until,
            clock_skew=timedelta(
                seconds=provider_state["clock_skew_seconds"]
            ),
            authority=authority,
        )
        if not self._control_plane.compare_and_swap(
            snapshot,
            replacement,
        ):
            return None, [
                "provider-state/admission CAS lost a concurrent transition"
            ]
        return authority, []

    def current_authority(
        self,
        expected: AdmissionExpectation,
    ) -> tuple[VerifiedAdmissionAuthority | None, list[str]]:
        try:
            snapshot = self._control_plane.read_snapshot()
        except Exception as error:
            return None, [
                "protected admission control plane is unavailable: {}".format(
                    error
                )
            ]
        head = snapshot.admission_head
        if head is None:
            return None, ["canonical GitHub admission has no committed head"]
        if head.provider_state_sha256 != canonical_sha256(
            dict(snapshot.provider_state)
        ):
            return None, [
                "canonical GitHub admission head is stale for provider state"
            ]
        now = self._verification_clock()
        if now.tzinfo is None:
            return None, ["verification time must include a timezone"]
        if now.astimezone(timezone.utc) > head.valid_until + head.clock_skew:
            return None, ["canonical GitHub admission head has expired"]
        authority = head.authority
        actual = (
            authority.policy_sha256,
            authority.authority_root_bundle_sha256,
            authority.verifier_release_sha256,
            dict(authority.evaluated_artifact_hashes),
        )
        wanted = (
            expected.policy_sha256,
            expected.authority_root_bundle_sha256,
            expected.verifier_release_sha256,
            dict(expected.evaluated_artifact_hashes),
        )
        if actual != wanted:
            return None, [
                "canonical GitHub admission does not match current repository"
            ]
        return authority, []
