# Gate 3 Behavior-first Evaluation Authority

## Purpose

Gate 3 tests whether a Business Analysis Agent can handle unfamiliar business questions without
deriving the test design from WAJE actions, tools, SQL, controller nodes, or one canonical
`AnalysisFrame`.

The authoring Episode describes:

```text
natural user conversation
+ business world and EvaluationClock
+ decision stakes
+ support expectation
+ acceptable outcome envelope
+ forbidden outcomes
+ atomic counterfactual relations
```

Review, partition, coverage promotion, grader choice, authority conformance, calibration and Gate
readiness live outside the Episode. An Episode cannot declare itself reviewed or promote itself.

## Current state

- 36 schema-valid WAJEgame Required Episodes in four explicit shards;
- 4 cross-domain transfer probes in a physically separate non-gating research
  lane;
- 8 real-user paid-amount questions from one source task;
- 19 multi-turn Episodes;
- 12 critical-risk Episodes;
- 120 structured counterfactual mutations; all 120 are mechanically replayable,
  claim-scoped and bound to deterministic materialized digests;
- all 36 Required Episodes have complete estimand, claim-case, boundary and
  replacement-expectation authoring;
- 41 frozen-real or controlled-fixture authorities are registered and
  hash-bound to materialized case files, including authorities reachable only
  through counterfactual variants;
- USER008 has one materialized prior-authority fixture plus a 19-milestone,
  19-observation append-only repair package;
- 36 independent-review packages;
- the Python 3.12.13 clean-copy suite passes 222 tests with 8
  environment-bound skips;
- G3.E0 remains `blocked`; `entry_decision=deny_g3_1`.
- G3.6.0 execution-authority development is in progress. The repository now
  contains hash-bound execution manifest, attempt, runtime-model-execution, trace,
  relation, hard-check, cell, suite and protected-receipt contracts. Formal
  admission remains blocked, and no real-provider full-matrix result exists.

The blocked state is intentional. All 41 case-file authorities still need
independent content-bound review. Source provenance, independent Episode double
review, identifiability review for 54 truth facts, grader calibration, a sealed
held-out set, promotion, a frozen run manifest and protected external admission
remain incomplete. Authoring completeness and materialization cannot promote
their own review or trust status.

## Protected trust boundary

Checked-in JSON and caller-controlled environment variables cannot authorize themselves. The local
verifier therefore rejects every configured authority root and emits
`external_admission_verified=blocked`. A local receipt file, registry edit, invented reviewer or
locally generated predecessor chain cannot unlock readiness.

The selected external issuer is GitHub Actions with public-repository Artifact Attestations backed
by Sigstore. `github-admission-request.schema.json`, `github-provider-state.schema.json`,
`gate3_admission_authority.py` and `github_gate3_admission.py` bind immutable numeric repository
identity, protected ref/event/environment, exact workflow/source revision, run/attempt, release and
trust epochs, admission and provider-state predecessors, complete evaluator release,
candidate-measured Python/dependency/import runtime and exact authorized Source/Review and manifest
hashes. One externally approved
`admission_authority_sha256` binds the release, runtime and authorization sections together.

The unprivileged job may execute repository code and has no OIDC or attestation permission. The
authority policy permits exactly one signing job. The privileged job performs no checkout and
executes no repository program. It validates the request against provisional protected environment
secrets before `actions/attest` signs the request as the Sigstore subject. Verification fixes
repository, signer workflow/digest, source ref/digest, OIDC issuer, SLSA predicate and the
GitHub-hosted runner certificate property.

The public remote, provider verification contract, candidate/attestation
workflow, canonical provider-state connector and atomic monotonic
admission/provider-state CAS contract exist and pass local attack tests.
Protected independent review, a trusted workflow revision, the first real
bundle, a trusted provider-state record and a digest-pinned hermetic builder
remain unprovisioned. The candidate runtime payload is measured and hash-bound;
it does not yet prove a complete runtime closure. Local commands accept no
caller-selected provider state, bundle, verified object or clock and continue
to emit `external_admission_verified=blocked`.

## Authority layout

```text
gate3/
├── evaluation-episode.schema.json
├── evaluation-views.schema.json
├── evaluation-run-result.schema.json
├── github-admission-request.schema.json
├── github-provider-state.schema.json
├── gate3-e0-trust.schema.json
├── gate3-eval-policy.json
├── gate3-execution-manifest.schema.json
├── gate3-execution-attempt-journal.schema.json
├── gate3-runtime-model-execution.schema.json
├── gate3-trace-bundle.schema.json
├── gate3-trace-artifact-index.schema.json
├── gate3-hard-check-result.schema.json
├── gate3-relation-result.schema.json
├── gate3-execution-cell-result.schema.json
├── gate3-suite-result.schema.json
├── gate3-run-execution-receipt.schema.json
├── taxonomy/
│   └── coverage-taxonomy.json
├── candidates/
├── case-files/
│   ├── case-file-authority.schema.json
│   └── case-file-authorities.json
├── authority/
├── research/
│   └── transfer-probes.json
├── catalog/
│   └── gate3-authoring-candidates.json
├── registries/
│   ├── source-registry.json
│   ├── review-registry.json
│   ├── corpus-registry.json
│   ├── grader-registry.json
│   └── mutation-operator-registry.json
├── profiles/
│   ├── authority-conformance-profiles.json
│   ├── cross-gate-world-profiles.json
│   └── execution-trace-profiles.json
├── promotion/
│   └── review-packages.json
├── calibration/
│   └── grader-calibration-package.json
├── runner-artifact-index.schema.json
├── manifests/
│   ├── promotion-manifest.json
│   ├── protected-held-out-manifest.json
│   └── run-manifest.json
├── coverage-ledger.json
└── gate3-e0-readiness.json
```

`candidates/` contains exactly four policy-allowlisted WAJEgame Required shards and is read-only to
the corpus generator. `research/` is unreachable from catalog, review, promotion, run and
readiness inputs. The exact-union catalog and registry/profile/review-package artifacts are
deterministic outputs.
Review, promotion, calibration, held-out and run manifests are authority-owned inputs; generation
commands cannot reset them.

## Agent and evaluator isolation

`compile_gate3_eval_views.py --check` validates both projections from explicit whitelists:

- `AgentWorldView` receives the current injected messages, already-reached business-visible events,
  frozen clock, provided context and governed semantic, data-probe and prior-authority inspection
  references;
- `EvaluatorOracleView` receives the complete message plan, truth facts, support expectation,
  scheduled-event authority expectations, suite binding, data-source bindings, outcome envelope,
  forbidden outcomes and grader/profile references.

The Agent view also excludes Episode IDs, core hashes and world-profile handles that could act as
oracle lookup keys. It excludes title, provenance, future messages, decision-stakes oracle text, truth,
acceptable outcomes, forbidden outcomes, siblings and grader authority. Canary tests cover future
message, truth, title and evaluator-only data-condition leakage. The CLI cannot emit an oracle or
accept a caller-selected future turn. Runtime process/credential separation is completed in G3.2.

## Three-layer verdict

Every run cell has independent:

1. product-behavior verdict;
2. authority-conformance verdict;
3. implementation verdict.

The final verdict is derived by strict AND. Every critical veto must bind a
registered failed child check and an indexed layer artifact. Every verdict,
including fail, blocked and invalid, binds the frozen manifest, the exact
grader-registry hash and a runner artifact index. Missing artifacts, a blocked
or invalid layer, or oracle leakage cannot be averaged away.

G3.6 keeps the Evaluation Reviewer blind to hard-check verdicts. Product
behavior is derived mechanically from the Reviewer's typed scores and critical
findings, and the Review must cover the exact product predicate set registered
for the run. Authority-conformance and implementation verdicts are derived from
the complete check sets registered for those layers. A cell cannot omit a
registered check or override the strict aggregate. The underlying local check
observations remain runner-self-attested during G3.6.0, so local hashes prove
repository-level consistency only. Formal admission requires a protected
external execution receipt and independent recomputation from the actual
artifact bytes.

Model-produced stages consume `RuntimeModelExecution` bundles projected from
the runtime store. Each bundle carries the complete persisted logical job,
ordered request/receipt history and durable result. Validation recomputes the
configuration, request artifact, provider body, output and record-set hashes;
binds the cell seed; and resolves stage role, job kind, input view and output
contract through the manifest-bound stage-producer registry. TraceArtifactIndex
v2 records the artifact kind and its authority source; producer identity is
derived from the durable result chain. A caller claiming `direct_store_read` or
`signed_export` cannot promote local evidence because protected source-proof
verification is still open.

For every `runtime_implemented` stage, the validator decodes the typed request
with the production dataclass contract and replays the production invocation
compiler. The replay must reproduce the provider body, input view, prompt,
tools and decoder identity exactly. The persisted RunTraceManifest also owns
the exact model-job, attempt-request, receipt and durable-result sets and is read from one
consistent storage snapshot. Historical partial manifests cannot project later
results. Runtime identities are globally unique across cells, and every
event-sourced stage binds the exact event type, cursor, authority/action and
event bytes. The Primary frame proposal, Reviewer candidate and acceptance
event must identify one frame. Canonical lane graphs and each model stage's full
producer capability tuple are enforced by an evaluator-code baseline, so a registry
cannot remove a review stage, shrink a producer contract or self-report an implementation.
Malformed authority, result collections and per-cell artifact maps derive an invalid/
blocked suite; missing or ghost cell keys cannot alter the evaluated set. Canonical answer-review and
evaluation-review producers remain `unprovisioned`; a manifest requiring them
is rejected before execution, and a unit-test double cannot change that
readiness state.

An execution attempt's artifact-set identity is recomputed from the complete
TraceArtifactIndex. Synchronizing arbitrary hashes across the attempt, cell and
hard-check artifacts cannot create a locally valid cell. This still does not
attest the underlying bytes or provider receipt; that trust boundary remains an
explicit protected-executor prerequisite.

For measurement gold, each claim target has its own design ceiling, and each
claim case has an evaluation turn, effective ceiling and local disposition. A truth
fact marked identifiable must cite eligible world facts the Agent can discover
and access; missing, conflicting, stale, denied or evaluator-only contracts
cannot identify it. A mechanically replayable counterfactual carries one
semantic intervention, exact non-null before/after values where applicable,
and a digest recomputed by replaying its atomic JSON Pointer patch set. Launch siblings
also bind every affected base claim-case digest, declare claim-local authority
effects and enumerate the untouched claims. A scope or time change may recompute
some claims while superseding claims that left the user request; these effects
remain claim-local and use a `mixed` aggregate summary. One semantic intervention
may update multiple authority surfaces only through an explicit
`composite_authority` patch set. Any replacement source authority reachable only
from a counterfactual still enters case-file materialization and independent
review readiness. This replay check does not approve business semantics; the two
independent reviewers own that decision.

## Lifecycle

1. Author candidate Episodes and business worlds.
2. Bind every claim to real, fixture or known-gap case-file sources.
3. Normalize and hash immutable Episode cores, including data-source bindings.
4. Materialize case-file artifacts, verify their digests, and obtain
   content-bound approvals from distinct business and measurement reviewers.
5. Resolve a verified Source Registry record.
6. Compile Agent/Evaluator views and cross-Gate profiles.
7. Obtain independent business-owner and measurement-reviewer records against the same core hash.
8. Promote reviewed Episodes through the PromotionManifest.
9. Calibrate graders using externally attested human calibration reviews.
   Every label binds the same Episode/core hash, immutable grader result and
   runner artifact index.
10. Seal an external held-out manifest without checked-in plaintext.
11. Freeze the run manifest with one base cell and every counterfactual sibling
    cell for each promoted Episode.
12. Obtain a provider-attested external admission and derive the read-only readiness manifest.
13. Allow G3.1 only when every condition passes.

No eval failure becomes a runtime rule automatically.

## Commands

From `vnext/`:

```bash
npm run generate:eval-corpus:gate3
npm run generate:eval-ledger:gate3
npm run generate:eval-readiness:gate3
npm run check:evals:gate3
npm run check:evals:gate3:policy-ready
npm run gate3:enter:g3.1
npm run check:eval-execution:gate3
.venv/bin/python tools/gate3_execution_authority.py <execution-manifest.json>
```

The three generation commands are explicit authoring actions. Both checks are read-only.
`check:evals:gate3` verifies structural integrity while preserving honest blocked conditions.
`check:evals:gate3:policy-ready` and `gate3:enter:g3.1` currently exit nonzero. Every future G3.1
entrypoint must depend on the latter hard gate.

The execution-authority command validates one G3.6 manifest. The canonical
candidate corpus still lacks reviewed paraphrases, operator scenarios and
protected execution inputs, so it cannot yet produce a formal G3.6 pass.
Synthetic unit fixtures and the local runtime-store projector exercise the
contracts without claiming corpus or protected-runtime readiness.
