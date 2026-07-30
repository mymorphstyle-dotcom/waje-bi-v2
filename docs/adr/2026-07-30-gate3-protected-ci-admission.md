# Gate 3 protected CI admission authority

## Status

Accepted on 2026-07-30.

## Decision

Gate 3 external evaluation authority uses a protected CI identity that signs a canonical
admission envelope. A dedicated online admission service remains a future option when WAJE needs
online revocation, multi-team issuance, or low-latency policy changes.

The repository owns:

- the strict JSON Schema for the protected trust policy and signed envelope;
- domain-separated canonical payload bytes;
- Ed25519 signature verification;
- exact policy, authority-root bundle, verifier-release and evaluated-artifact bindings;
- expiry, source revision, protected ref, workflow and run-identity checks;
- conversion of a valid envelope into immutable authorized Source/Review and manifest hash sets;
- final readiness derivation and fail-closed G3.1 admission.

The protected CI control plane owns:

- the signing key or KMS operation;
- the trust policy and public-key rotation;
- the authenticated repository, commit, protected ref, workflow revision and run identity;
- branch/environment protection and workflow approval policy;
- issuance, storage and audit retention of each signed envelope.

No private key, provisional public key or trust-policy fallback is stored in this repository.
Ordinary environment variables, caller-selected files and repository-local receipts cannot supply
the protected context. The local `gate3:enter:g3.1` command therefore remains blocked. The canonical
readiness API accepts no path, key, context, clock or verified-object arguments. It will continue to
deny external admission until a concrete CI provider adapter supplies control-plane provenance,
monotonic trust state and runtime attestation.

## Signed payload

The envelope binds:

- trust domain, audience, issuer and key identity;
- protected trust-policy hash/epoch and key-validity window;
- repository identity, exact source revision, protected ref, immutable workflow revision, protected
  runner release, run ID and run attempt;
- issuance and expiry;
- canonical Gate 3 policy hash;
- canonical reviewer/source/manifest authority-root bundle hash;
- verifier release hash, including schemas and verifier code;
- Python version, dependency declaration and exact dependency lock;
- the exact evaluated artifact hash map;
- authorized Source/Review record hashes;
- authorized promotion, calibration, held-out and run-manifest hashes.

The signature uses Ed25519 over:

```text
WAJE-GATE3-EXTERNAL-ADMISSION-V1\0
+ canonical JSON payload
```

Replaying an envelope against another commit, ref, workflow revision, runner release, run attempt,
policy, root bundle, verifier release or artifact set fails. Trust-policy rotation, expired keys,
expired envelopes and overlong envelopes fail.

## Operational activation

The repository has no CI provider configuration today. Provider onboarding must provision the
protected trust policy and signing identity outside repository job control, then connect the
protected runner to the canonical Gate.

The adapter acceptance suite must prove:

- issuer identity from the provider control plane;
- immutable repository, commit, protected ref, workflow revision, run ID and attempt;
- monotonic trust-policy epoch and current key revocation state;
- read-only input provenance from a protected mount/artifact channel, including directory and
  privilege boundaries;
- immutable runner image/release digest and the actual Python/interpreter/dependency/import
  provenance used by the verifier;
- a provider-owned current clock;
- protected status publication that untrusted jobs cannot forge or overwrite.

Key provisioning and the first real signed envelope are operational Gate 3 E0 preconditions. They
do not change this architectural decision.

## Consequences

- G3.1 stays blocked until real sources, reviewers, manifests and a protected envelope all pass.
- Local testing may use ephemeral test keys to exercise the contract; test keys never enter a Gate
  authority artifact.
- Changing verifier code or a bound schema invalidates prior admission envelopes.
- CI provider migration changes the protected adapter and trust policy while preserving the
  canonical repository contract.
