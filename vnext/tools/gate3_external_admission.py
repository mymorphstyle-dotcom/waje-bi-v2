#!/usr/bin/env python3
"""Protected-CI admission contract for Gate 3 evaluation authority.

The repository owns canonicalization and verification. A protected runner owns
the trust policy, current runner context, and signing key. This module has no
CLI and never reads trust roots from environment variables or repository files.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from jsonschema import Draft202012Validator, FormatChecker


SIGNATURE_DOMAIN = b"WAJE-GATE3-EXTERNAL-ADMISSION-V1\x00"


@dataclass(frozen=True)
class ProtectedCIContext:
    """Runner claims obtained from the protected CI control plane."""

    repository_id: str
    source_revision: str
    protected_ref: str
    workflow_ref: str
    workflow_revision_sha256: str
    runner_release_sha256: str
    run_id: str
    run_attempt: int


@dataclass(frozen=True)
class AdmissionExpectation:
    """Exact repository state the external admission must authorize."""

    policy_sha256: str
    authority_root_bundle_sha256: str
    verifier_release_sha256: str
    evaluated_artifact_hashes: Mapping[str, str]


@dataclass(frozen=True)
class VerifiedExternalAdmission:
    """Hash-bound authority returned only after signature verification."""

    issuer_id: str
    key_id: str
    envelope_sha256: str
    policy_sha256: str
    authority_root_bundle_sha256: str
    verifier_release_sha256: str
    evaluated_artifact_hashes: Mapping[str, str]
    authorized_attestation_sha256s: frozenset[str]
    authorized_manifest_sha256s: frozenset[str]
    protected_context: ProtectedCIContext
    issued_at: datetime
    expires_at: datetime


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def signature_message(payload: Mapping[str, Any]) -> bytes:
    return SIGNATURE_DOMAIN + canonical_json_bytes(payload)


def canonical_file_set_sha256(
    paths: Sequence[Path], *, relative_to: Path
) -> str:
    files = {
        str(path.resolve().relative_to(relative_to.resolve())): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }
    return canonical_sha256(files)


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


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def verify_external_admission(
    envelope: Mapping[str, Any],
    *,
    trust_policy: Mapping[str, Any],
    protected_context: ProtectedCIContext,
    expectation: AdmissionExpectation,
    schema: Mapping[str, Any],
) -> tuple[VerifiedExternalAdmission | None, list[str]]:
    """Verify one envelope against externally supplied protected authority."""

    findings = _schema_findings(
        trust_policy,
        schema,
        label="protected CI trust policy",
    )
    findings.extend(
        _schema_findings(
            envelope,
            schema,
            label="external admission envelope",
        )
    )
    if findings:
        return None, findings

    payload = envelope["payload"]
    signature = envelope["signature"]
    exact_matches = {
        "trust_domain": trust_policy["trust_domain"],
        "audience": trust_policy["audience"],
        "issuer_id": trust_policy["issuer_id"],
        "key_id": trust_policy["key_id"],
        "trust_policy_sha256": canonical_sha256(trust_policy),
        "repository_id": protected_context.repository_id,
        "source_revision": protected_context.source_revision,
        "protected_ref": protected_context.protected_ref,
        "workflow_ref": protected_context.workflow_ref,
        "workflow_revision_sha256": (
            protected_context.workflow_revision_sha256
        ),
        "runner_release_sha256": (
            protected_context.runner_release_sha256
        ),
        "run_id": protected_context.run_id,
        "run_attempt": protected_context.run_attempt,
        "policy_sha256": expectation.policy_sha256,
        "authority_root_bundle_sha256": (
            expectation.authority_root_bundle_sha256
        ),
        "verifier_release_sha256": expectation.verifier_release_sha256,
        "evaluated_artifact_hashes": dict(
            expectation.evaluated_artifact_hashes
        ),
    }
    for field, expected in exact_matches.items():
        if payload[field] != expected:
            findings.append(
                "external admission {} does not match protected context".format(
                    field
                )
            )

    if protected_context.repository_id != trust_policy["repository_id"]:
        findings.append("protected runner repository is not trusted")
    if protected_context.workflow_ref != trust_policy["workflow_ref"]:
        findings.append("protected runner workflow is not trusted")
    if (
        protected_context.workflow_revision_sha256
        != trust_policy["workflow_revision_sha256"]
    ):
        findings.append("protected runner workflow revision is not trusted")
    if (
        protected_context.runner_release_sha256
        != trust_policy["runner_release_sha256"]
    ):
        findings.append("protected runner release is not trusted")
    if (
        protected_context.protected_ref
        not in trust_policy["allowed_protected_refs"]
    ):
        findings.append("protected runner ref is not authorized")
    if signature["key_id"] != trust_policy["key_id"]:
        findings.append("signature key does not match trusted key")
    if signature["key_id"] != payload["key_id"]:
        findings.append("signature key does not match payload key")
    if signature["algorithm"] != trust_policy["algorithm"]:
        findings.append("signature algorithm does not match trust policy")

    issued_at = _parse_timestamp(payload["issued_at"])
    expires_at = _parse_timestamp(payload["expires_at"])
    key_valid_from = _parse_timestamp(trust_policy["key_valid_from"])
    key_valid_until = _parse_timestamp(trust_policy["key_valid_until"])
    current_time = datetime.now(timezone.utc)
    if issued_at is None or expires_at is None:
        findings.append("admission timestamps must be timezone-aware")
    elif expires_at <= issued_at:
        findings.append("admission expiry must follow issuance")
    else:
        validity_seconds = int((expires_at - issued_at).total_seconds())
        if validity_seconds > trust_policy["maximum_validity_seconds"]:
            findings.append("admission validity exceeds trust policy")
        if issued_at > current_time:
            findings.append("admission is not valid yet")
        if current_time >= expires_at:
            findings.append("admission has expired")
    if key_valid_from is None or key_valid_until is None:
        findings.append("trusted key timestamps must be timezone-aware")
    elif key_valid_until <= key_valid_from:
        findings.append("trusted key expiry must follow activation")
    else:
        if current_time < key_valid_from:
            findings.append("trusted key is not active yet")
        if current_time >= key_valid_until:
            findings.append("trusted key has expired")
        if issued_at is not None and issued_at < key_valid_from:
            findings.append("admission predates trusted key activation")
        if expires_at is not None and expires_at > key_valid_until:
            findings.append("admission outlives trusted key")

    try:
        public_key_bytes = base64.b64decode(
            trust_policy["public_key_base64"],
            validate=True,
        )
        signature_bytes = base64.b64decode(
            signature["value_base64"],
            validate=True,
        )
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature_bytes,
            signature_message(payload),
        )
    except (
        ValueError,
        binascii.Error,
        InvalidSignature,
    ):
        findings.append("external admission signature is invalid")

    if findings:
        return None, findings

    return (
        VerifiedExternalAdmission(
            issuer_id=payload["issuer_id"],
            key_id=payload["key_id"],
            envelope_sha256=canonical_sha256(envelope),
            policy_sha256=payload["policy_sha256"],
            authority_root_bundle_sha256=payload[
                "authority_root_bundle_sha256"
            ],
            verifier_release_sha256=payload["verifier_release_sha256"],
            evaluated_artifact_hashes=MappingProxyType(
                dict(payload["evaluated_artifact_hashes"])
            ),
            authorized_attestation_sha256s=frozenset(
                payload["authorized_attestation_sha256s"]
            ),
            authorized_manifest_sha256s=frozenset(
                payload["authorized_manifest_sha256s"]
            ),
            protected_context=protected_context,
            issued_at=issued_at,
            expires_at=expires_at,
        ),
        [],
    )
