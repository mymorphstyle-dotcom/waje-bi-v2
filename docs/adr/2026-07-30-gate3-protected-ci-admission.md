# Gate 3 GitHub Actions / Sigstore admission authority

## Status

Accepted on 2026-07-30 and amended after provider selection.

The development repository is public at
`https://github.com/mymorphstyle-dotcom/waje-bi-v2`. GitHub repository ID
`1317104320` and owner ID `278493004` are immutable provider identities.

## Decision

Gate 3 development admission uses GitHub Actions workload identity and GitHub Artifact
Attestations backed by Sigstore. The earlier raw Ed25519 envelope profile has been removed from the
current contract. There are no production consumers that require compatibility.

The selected trust chain is:

```text
unprivileged candidate job
→ canonical admission request
→ protected GitHub environment state
→ privileged attestation job
→ GitHub OIDC + Sigstore bundle
→ strict gh attestation verification
→ provider-neutral verified admission authority
→ canonical Gate readiness
```

The unprivileged job may execute repository code. It has `contents: read` and has no OIDC or
attestation permission. The privileged job does not checkout or execute repository code. It
downloads the same-run request, validates it using code embedded in the attested workflow, compares
its complete admission-authority hash with protected environment secrets, and only then invokes the
SHA-pinned `actions/attest`. Those secrets are an unprovisioned first adapter; the canonical
provider-state reader and atomic state service remain activation requirements.

GitHub documents that an attestation predicate can be controlled by the originating workflow. WAJE
therefore treats the request file digest and signer certificate identity as the authority surface.
The complete request is the Sigstore subject. A trusted builder and an externally approved
admission-authority hash are mandatory.

## Bound identities

The admission request and verification policy bind:

- `mymorphstyle-dotcom/waje-bi-v2` plus numeric repository and owner IDs;
- exact 40-character source revision and `refs/heads/main`;
- `push` event and `gate3-admission` environment;
- exact workflow path and workflow commit;
- GitHub-hosted runner requirement;
- run ID, run attempt and operation ID;
- release epoch and minimum trust-policy epoch;
- predecessor admission hash;
- policy, authority-root bundle and verifier-release hashes;
- every evaluated artifact hash;
- Python 3.12 patch release and candidate-observed interpreter digest;
- installed dependency inventory, critical import origins and evaluated source-tree digest;
- exact authorized Source/Review and manifest hashes.

`admission_authority_sha256` covers the release authority, the candidate-measured
`runtime_attestation` payload and both authorization sets. The candidate cannot change a measured
runtime digest or authorization and retain the externally approved identity. These measurements
are hash-bound observations; hosted runner labels, executable hashes and dependency inventories do
not establish a hermetic runtime closure. Activation therefore also requires a digest-pinned
builder image and a reviewed closure over the interpreter, native libraries and Node runtime.
The privileged job validates the complete admission digest; signing alone does not turn candidate
assertions into trusted facts.

Verification calls `gh attestation verify` with exact repository, signer workflow, signer digest,
source digest, source ref, GitHub OIDC issuer, SLSA predicate type, public GitHub host and
`--deny-self-hosted-runners`. The verifier also requires the verified certificate summary to carry
`runnerEnvironment=github-hosted`. The `gh` executable is an absolute regular file whose SHA-256
must match approved provider state. Request and bundle bytes are opened without following symlinks,
copied into a private verification directory and hash-checked before and after the subprocess.

## Provider-owned state

`github-provider-state.schema.json` defines the external monotonic state:

- trusted workflow revision;
- current release epoch and approved admission-authority hash;
- approved GitHub CLI executable hash;
- minimum trust-policy epoch;
- previous admission hash;
- previous provider-state hash;
- state version.

Repository files and `GITHUB_*` environment variables cannot supply this authority to the canonical
Gate. The protected job reads protected environment secrets only as the first GitHub control-plane
adapter. Production-grade repeated issuance still requires atomic compare-and-swap of state version,
provider-state predecessor and admission predecessor through a GitHub App or external ledger.

The provider verifier can produce a provider-neutral `VerifiedAdmissionAuthority`. A trusted
canonical connector that obtains provider state from the protected control plane, verifies the real
bundle and passes only that verified value into readiness has not been provisioned. Local
environment variables and caller-selected JSON remain ineligible for that role.

## Workflow boundaries

`.github/workflows/vnext-ci.yml` covers pull requests, merge queue and main pushes. It has read-only
contents permission.

`.github/workflows/gate3-protected-admission.yml` accepts only a push to `main`:

1. `candidate` runs tests and builds the request without OIDC or attestation rights.
2. `attest` enters the `gate3-admission` environment, receives only the minimum GitHub permissions,
   downloads the same-run artifact, applies protected-state checks, signs the exact request and
   uploads the Sigstore bundle.

The authority policy requires the exact job set `{candidate, attest}`. It rejects job-level reusable
workflows, any second job with elevated permissions and any `actions/attest` call outside `attest`.
All actions use full commit SHAs. The privileged job has no checkout, cache restore, inherited
secret or repository program execution. `working-directory: vnext` is a lintable workflow
convention; it is not a process sandbox.

GitHub requires workflows under the repository-root `.github/` directory. These files are the only
root-level vNext deployment projection. Their exact paths and hashes are owned by
`vnext/ops/github/workflow-authority-policy.json`; the Day 0 clean-copy verifier copies and validates
that projection together with `vnext/`.

## Current activation boundary

Implemented in the repository:

- public GitHub remote and immutable numeric identity;
- provider-state and admission-request schemas;
- runtime, authorization and complete admission-authority binding;
- strict Sigstore verification adapter;
- privilege-separated workflows;
- workflow-policy and provider attack tests;
- protected `main` baseline and a `gate3-admission` environment restricted to protected branches;
- strict `vNext validation` required check bound to the GitHub Actions app;
- canonical local Gate remains fail closed.

Still required before `external_admission_verified` may pass:

- merge the reviewed workflow into protected `main`;
- add an independent environment reviewer, prevent self-review and disable administrator bypass;
- provision the protected environment secrets as a first adapter and pin the first approved
  admission-authority hash and workflow revision outside the candidate;
- produce and independently verify the first real Sigstore bundle;
- provision the trusted canonical provider-state reader/connector into readiness;
- add atomic monotonic receipt/state CAS for repeated issuance;
- run admission in a digest-pinned hermetic builder and bind its complete runtime closure;
- provision reviewed trusted-root and freshness policy for offline verification;
- complete Source/Review, measurement-gold, calibration, held-out and run-manifest gates.

G3.1 remains `deny_g3_1` until every strict-AND condition passes.

## Consequences

- No long-lived signing key is exposed to a GitHub runner.
- Fork PRs, `pull_request_target`, `workflow_run`, manual arbitrary-ref dispatch and self-hosted
  runners cannot enter the admission workflow.
- Changing a bound policy, schema, dependency, runtime, authorization, workflow or verifier produces a new admission-authority
  hash and requires an external approval update.
- A valid Sigstore bundle proves producer identity and subject integrity. WAJE continues to evaluate
  the request’s business/evaluation authority separately.
