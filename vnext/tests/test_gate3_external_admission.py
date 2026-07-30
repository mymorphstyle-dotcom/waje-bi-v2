from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)


VNEXT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNEXT_ROOT / "tools"))

from gate3_external_admission import (  # noqa: E402
    AdmissionExpectation,
    ProtectedCIContext,
    VerifiedExternalAdmission,
    canonical_sha256,
    signature_message,
    verify_external_admission,
)
from verify_gate3_e0 import (  # noqa: E402
    POLICY_PATH,
    _authority_roots,
    build_external_admission_expectation,
    compute_readiness,
)


ADMISSION_SCHEMA_PATH = (
    VNEXT_ROOT
    / "evals"
    / "gate3"
    / "gate3-admission-envelope.schema.json"
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Gate3ExternalAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.schema = _load_json(ADMISSION_SCHEMA_PATH)
        self.context = ProtectedCIContext(
            repository_id="waje/waje-bi-v2",
            source_revision="a" * 40,
            protected_ref="refs/heads/main",
            workflow_ref="waje/gate3-admission",
            workflow_revision_sha256="6" * 64,
            runner_release_sha256="7" * 64,
            run_id="protected-run-42",
            run_attempt=1,
        )
        self.trust_policy = {
            "artifact_type": "gate3_protected_ci_trust_policy",
            "artifact_version": "gate3.protected-ci-trust-policy.v1",
            "trust_policy_id": "waje-gate3-protected-ci",
            "trust_policy_epoch": 1,
            "trust_domain": "waje-release",
            "audience": "waje-gate3-e0",
            "repository_id": self.context.repository_id,
            "issuer_id": "waje-protected-ci",
            "key_id": "kms-ed25519-2026-01",
            "algorithm": "ed25519",
            "public_key_base64": base64.b64encode(public_key).decode(),
            "key_valid_from": (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(),
            "key_valid_until": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
            "workflow_ref": self.context.workflow_ref,
            "workflow_revision_sha256": (
                self.context.workflow_revision_sha256
            ),
            "runner_release_sha256": (
                self.context.runner_release_sha256
            ),
            "allowed_protected_refs": [self.context.protected_ref],
            "maximum_validity_seconds": 600,
        }
        self.expectation = AdmissionExpectation(
            policy_sha256="b" * 64,
            authority_root_bundle_sha256="c" * 64,
            verifier_release_sha256="d" * 64,
            evaluated_artifact_hashes={
                "gate3-eval-policy.json": "e" * 64,
                "tools/verify_gate3_e0.py": "f" * 64,
            },
        )
        self.now = datetime.now(timezone.utc)

    def _envelope(
        self,
        *,
        expectation: AdmissionExpectation | None = None,
        context: ProtectedCIContext | None = None,
    ) -> dict:
        expectation = expectation or self.expectation
        context = context or self.context
        payload = {
            "trust_domain": self.trust_policy["trust_domain"],
            "audience": self.trust_policy["audience"],
            "issuer_id": self.trust_policy["issuer_id"],
            "key_id": self.trust_policy["key_id"],
            "repository_id": context.repository_id,
            "source_revision": context.source_revision,
            "protected_ref": context.protected_ref,
            "workflow_ref": context.workflow_ref,
            "workflow_revision_sha256": (
                context.workflow_revision_sha256
            ),
            "runner_release_sha256": (
                context.runner_release_sha256
            ),
            "run_id": context.run_id,
            "run_attempt": context.run_attempt,
            "issued_at": (
                self.now - timedelta(seconds=30)
            ).isoformat(),
            "expires_at": (
                self.now + timedelta(minutes=5)
            ).isoformat(),
            "trust_policy_sha256": canonical_sha256(
                self.trust_policy
            ),
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
            "authorized_attestation_sha256s": ["1" * 64],
            "authorized_manifest_sha256s": ["2" * 64],
        }
        signature = self.private_key.sign(signature_message(payload))
        return {
            "artifact_type": "gate3_external_admission_envelope",
            "artifact_version": "gate3.external-admission.v1",
            "payload": payload,
            "signature": {
                "algorithm": "ed25519",
                "key_id": self.trust_policy["key_id"],
                "value_base64": base64.b64encode(signature).decode(),
            },
        }

    def _verify(
        self,
        envelope: dict,
        *,
        context: ProtectedCIContext | None = None,
        expectation: AdmissionExpectation | None = None,
        trust_policy: dict | None = None,
    ) -> tuple[VerifiedExternalAdmission | None, list[str]]:
        return verify_external_admission(
            envelope,
            trust_policy=trust_policy or self.trust_policy,
            protected_context=context or self.context,
            expectation=expectation or self.expectation,
            schema=self.schema,
        )

    def test_valid_envelope_returns_hash_bound_authority(self) -> None:
        envelope = self._envelope()
        verified, findings = self._verify(envelope)
        self.assertEqual([], findings)
        self.assertIsNotNone(verified)
        self.assertEqual(
            canonical_sha256(envelope),
            verified.envelope_sha256,
        )
        self.assertEqual(
            frozenset({"1" * 64}),
            verified.authorized_attestation_sha256s,
        )
        self.assertEqual(
            frozenset({"2" * 64}),
            verified.authorized_manifest_sha256s,
        )

    def test_payload_tampering_invalidates_signature(self) -> None:
        envelope = self._envelope()
        envelope["payload"]["authorized_attestation_sha256s"] = ["3" * 64]
        verified, findings = self._verify(envelope)
        self.assertIsNone(verified)
        self.assertIn(
            "external admission signature is invalid",
            findings,
        )

    def test_protected_context_rejects_replay(self) -> None:
        envelope = self._envelope()
        attacks = {
            "source_revision": ProtectedCIContext(
                **{
                    **self.context.__dict__,
                    "source_revision": "9" * 40,
                }
            ),
            "protected_ref": ProtectedCIContext(
                **{
                    **self.context.__dict__,
                    "protected_ref": "refs/heads/unprotected",
                }
            ),
            "workflow_ref": ProtectedCIContext(
                **{
                    **self.context.__dict__,
                    "workflow_ref": "fork/untrusted@v1",
                }
            ),
            "workflow_revision_sha256": ProtectedCIContext(
                **{
                    **self.context.__dict__,
                    "workflow_revision_sha256": "8" * 64,
                }
            ),
            "runner_release_sha256": ProtectedCIContext(
                **{
                    **self.context.__dict__,
                    "runner_release_sha256": "9" * 64,
                }
            ),
            "run_id": ProtectedCIContext(
                **{
                    **self.context.__dict__,
                    "run_id": "replayed-run",
                }
            ),
            "run_attempt": ProtectedCIContext(
                **{
                    **self.context.__dict__,
                    "run_attempt": 2,
                }
            ),
        }
        for field, attacked_context in attacks.items():
            with self.subTest(field=field):
                verified, findings = self._verify(
                    envelope,
                    context=attacked_context,
                )
                self.assertIsNone(verified)
                self.assertTrue(
                    any(field in finding for finding in findings)
                    or any("not trusted" in finding for finding in findings)
                    or any(
                        "not authorized" in finding
                        for finding in findings
                    )
                )

    def test_stale_artifact_or_verifier_binding_is_rejected(self) -> None:
        envelope = self._envelope()
        for field, attacked in (
            (
                "evaluated_artifact_hashes",
                AdmissionExpectation(
                    policy_sha256=self.expectation.policy_sha256,
                    authority_root_bundle_sha256=(
                        self.expectation.authority_root_bundle_sha256
                    ),
                    verifier_release_sha256=(
                        self.expectation.verifier_release_sha256
                    ),
                    evaluated_artifact_hashes={"changed.py": "0" * 64},
                ),
            ),
            (
                "verifier_release_sha256",
                AdmissionExpectation(
                    policy_sha256=self.expectation.policy_sha256,
                    authority_root_bundle_sha256=(
                        self.expectation.authority_root_bundle_sha256
                    ),
                    verifier_release_sha256="0" * 64,
                    evaluated_artifact_hashes=(
                        self.expectation.evaluated_artifact_hashes
                    ),
                ),
            ),
        ):
            with self.subTest(field=field):
                verified, findings = self._verify(
                    envelope,
                    expectation=attacked,
                )
                self.assertIsNone(verified)
                self.assertTrue(
                    any(field in finding for finding in findings)
                )

    def test_trust_policy_rotation_invalidates_old_envelope(self) -> None:
        envelope = self._envelope()
        rotated = copy.deepcopy(self.trust_policy)
        rotated["trust_policy_epoch"] = 2
        verified, findings = self._verify(
            envelope,
            trust_policy=rotated,
        )
        self.assertIsNone(verified)
        self.assertTrue(
            any("trust_policy_sha256" in item for item in findings)
        )

    def test_verifier_release_binds_python_and_dependencies(self) -> None:
        policy = _load_json(POLICY_PATH)
        expectation = build_external_admission_expectation(policy)
        self.assertIn(".python-version", expectation.evaluated_artifact_hashes)
        self.assertIn("pyproject.toml", expectation.evaluated_artifact_hashes)
        self.assertIn("uv.lock", expectation.evaluated_artifact_hashes)

    def test_expired_or_overlong_envelope_is_rejected(self) -> None:
        expired = self._envelope()
        expired["payload"]["issued_at"] = (
            self.now - timedelta(minutes=30)
        ).isoformat()
        expired["payload"]["expires_at"] = (
            self.now - timedelta(minutes=10)
        ).isoformat()
        expired["signature"]["value_base64"] = base64.b64encode(
            self.private_key.sign(signature_message(expired["payload"]))
        ).decode()
        verified, findings = self._verify(expired)
        self.assertIsNone(verified)
        self.assertIn("admission has expired", findings)
        self.assertIn("admission validity exceeds trust policy", findings)

    def test_expired_trusted_key_is_rejected(self) -> None:
        expired_policy = copy.deepcopy(self.trust_policy)
        expired_policy["key_valid_from"] = (
            self.now - timedelta(days=2)
        ).isoformat()
        expired_policy["key_valid_until"] = (
            self.now - timedelta(days=1)
        ).isoformat()
        envelope = self._envelope()
        envelope["payload"]["trust_policy_sha256"] = canonical_sha256(
            expired_policy
        )
        envelope["signature"]["value_base64"] = base64.b64encode(
            self.private_key.sign(signature_message(envelope["payload"]))
        ).decode()
        verified, findings = self._verify(
            envelope,
            trust_policy=expired_policy,
        )
        self.assertIsNone(verified)
        self.assertIn("trusted key has expired", findings)

    def test_local_environment_cannot_supply_admission_authority(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WAJE_GATE3_ADMISSION_ENVELOPE": "/tmp/self-signed.json",
                "WAJE_GATE3_TRUST_POLICY": "/tmp/self-selected-key.json",
            },
            clear=False,
        ):
            readiness, _ = compute_readiness()
        external = next(
            condition
            for condition in readiness["condition_verdicts"]
            if condition["condition_id"] == "external_admission_verified"
        )
        self.assertEqual("blocked", external["verdict"])

    def test_canonical_gate_rejects_injected_verified_object(self) -> None:
        forged = VerifiedExternalAdmission(
            issuer_id="local",
            key_id="local",
            envelope_sha256="0" * 64,
            policy_sha256="0" * 64,
            authority_root_bundle_sha256="0" * 64,
            verifier_release_sha256="0" * 64,
            evaluated_artifact_hashes={},
            authorized_attestation_sha256s=frozenset(),
            authorized_manifest_sha256s=frozenset(),
            protected_context=self.context,
            issued_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=1),
        )
        with self.assertRaises(TypeError):
            compute_readiness(verified_external_admission=forged)

    def test_signed_envelope_cannot_unlock_unprovisioned_canonical_gate(
        self,
    ) -> None:
        policy = _load_json(POLICY_PATH)
        expectation = build_external_admission_expectation(policy)
        envelope = self._envelope(expectation=expectation)
        verified, findings = self._verify(
            envelope,
            expectation=expectation,
        )
        self.assertEqual([], findings)
        self.assertIsNotNone(verified)
        readiness, _ = compute_readiness()
        conditions = {
            condition["condition_id"]: condition["verdict"]
            for condition in readiness["condition_verdicts"]
        }
        self.assertEqual(
            "blocked",
            conditions["external_admission_verified"],
        )
        self.assertEqual("blocked", readiness["derived_status"])
        self.assertEqual("deny_g3_1", readiness["entry_decision"])

    def test_signed_root_bundle_still_requires_receipt_integrity(self) -> None:
        policy = _load_json(POLICY_PATH)
        attacked = copy.deepcopy(policy)
        with tempfile.TemporaryDirectory() as temp_root:
            workspace_root = Path(temp_root)
            receipt_path = workspace_root / "source-root.json"
            receipt_path.write_text(
                '{"issuer":"protected-ci"}\n',
                encoding="utf-8",
            )
            receipt_sha256 = hashlib.sha256(
                receipt_path.read_bytes()
            ).hexdigest()
            attacked["corpus_authority"]["source_authority_roots"] = [
                {
                    "authority_root_id": "ROOT-PROTECTED-SOURCE",
                    "receipt_ref": "source-root.json",
                    "receipt_sha256": receipt_sha256,
                    "authorized_source_pools": [
                        "generated_business_world"
                    ],
                }
            ]
            attacked_expectation = AdmissionExpectation(
                policy_sha256=canonical_sha256(attacked),
                authority_root_bundle_sha256=canonical_sha256(
                    {
                        key: attacked["corpus_authority"][key]
                        for key in (
                            "reviewer_authority_roots",
                            "source_authority_roots",
                            "manifest_authority_roots",
                        )
                    }
                ),
                verifier_release_sha256=self.expectation.verifier_release_sha256,
                evaluated_artifact_hashes=(
                    self.expectation.evaluated_artifact_hashes
                ),
            )
            envelope = self._envelope(expectation=attacked_expectation)
            verified, findings = self._verify(
                envelope,
                expectation=attacked_expectation,
            )
            self.assertEqual([], findings)
            roots, root_findings = _authority_roots(
                attacked,
                workspace_root=workspace_root,
                external_admission=verified,
            )
            self.assertEqual([], root_findings)
            self.assertIn(
                "ROOT-PROTECTED-SOURCE",
                roots["source_authority_roots"],
            )
            receipt_path.write_text(
                '{"issuer":"tampered"}\n',
                encoding="utf-8",
            )
            roots, root_findings = _authority_roots(
                attacked,
                workspace_root=workspace_root,
                external_admission=verified,
            )
            self.assertEqual({}, roots["source_authority_roots"])
            self.assertTrue(
                any("receipt hash is stale" in item for item in root_findings)
            )


if __name__ == "__main__":
    unittest.main()
