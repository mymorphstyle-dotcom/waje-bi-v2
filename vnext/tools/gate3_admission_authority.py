#!/usr/bin/env python3
"""Provider-neutral value objects and canonical hashing for Gate 3."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class AdmissionExpectation:
    """Exact repository state an external provider may authorize."""

    policy_sha256: str
    authority_root_bundle_sha256: str
    verifier_release_sha256: str
    evaluated_artifact_hashes: Mapping[str, str]


@dataclass(frozen=True)
class VerifiedAdmissionAuthority:
    """Provider-neutral authority consumed by readiness internals."""

    issuer_id: str
    authority_key_id: str
    receipt_sha256: str
    authority_state_sha256: str
    authority_state_version: int
    predecessor_receipt_sha256: str | None
    policy_sha256: str
    authority_root_bundle_sha256: str
    verifier_release_sha256: str
    evaluated_artifact_hashes: Mapping[str, str]
    authorized_attestation_sha256s: frozenset[str]
    authorized_manifest_sha256s: frozenset[str]

    @classmethod
    def create(
        cls,
        *,
        issuer_id: str,
        authority_key_id: str,
        receipt_sha256: str,
        authority_state_sha256: str,
        authority_state_version: int,
        predecessor_receipt_sha256: str | None,
        expectation: AdmissionExpectation,
        authorized_attestation_sha256s: frozenset[str],
        authorized_manifest_sha256s: frozenset[str],
    ) -> "VerifiedAdmissionAuthority":
        return cls(
            issuer_id=issuer_id,
            authority_key_id=authority_key_id,
            receipt_sha256=receipt_sha256,
            authority_state_sha256=authority_state_sha256,
            authority_state_version=authority_state_version,
            predecessor_receipt_sha256=predecessor_receipt_sha256,
            policy_sha256=expectation.policy_sha256,
            authority_root_bundle_sha256=(
                expectation.authority_root_bundle_sha256
            ),
            verifier_release_sha256=expectation.verifier_release_sha256,
            evaluated_artifact_hashes=MappingProxyType(
                dict(expectation.evaluated_artifact_hashes)
            ),
            authorized_attestation_sha256s=(
                authorized_attestation_sha256s
            ),
            authorized_manifest_sha256s=authorized_manifest_sha256s,
        )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_file_set_sha256(
    paths: Sequence[Path], *, relative_to: Path
) -> str:
    root = relative_to.resolve()
    files: dict[str, str] = {}
    for path in paths:
        resolved = path.resolve(strict=True)
        relative_path = str(resolved.relative_to(root))
        if relative_path in files:
            raise ValueError(
                "canonical file set contains duplicate path {}".format(
                    relative_path
                )
            )
        if path.is_symlink():
            raise ValueError(
                "canonical file set cannot contain symlink {}".format(path)
            )
        files[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return canonical_sha256(files)
