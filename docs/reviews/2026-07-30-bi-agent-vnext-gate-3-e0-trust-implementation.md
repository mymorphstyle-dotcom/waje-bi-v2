# WAJE BI Agent vNext Gate 3 E0 trust foundation

## 1. Gate verdict

| 项目 | 状态 |
|---|---|
| Authoring contract | Complete for E0 infrastructure |
| Source/Review authority | Implemented; real records incomplete |
| Protected CI admission | Verification contract and candidate/attestation workflow implemented; canonical provider connector and remote activation incomplete |
| Agent/Evaluator projection contract | Complete; runtime process isolation belongs to G3.2 |
| Grader/Authority profile contract | Complete |
| Three-layer result contract | Complete |
| Independent review and calibration | Blocked |
| G3.1 entry | `deny_g3_1` |
| Entry interview | 用户确认 public GitHub remote 与 GitHub Artifact Attestations/Sigstore；真实 reviewer 身份和来源证明出现前不伪造签字 |

## 2. Closed infrastructure findings

### E0-C1. Episode self-promotion

`EvaluationEpisode` v2 no longer owns `review_status`、`dataset_partition`、review attestation or
grading plan. Immutable review-subject hash covers source pool、source binding、coverage tags、
conversation、business world、decision stakes、support expectation、outcome envelope、forbidden
outcomes and counterfactuals. Authoring notes remain outside that hash. Review、partition and
promotion live in external registries/manifests.

### E0-C2. Baseline support escape

Every Episode now carries:

- baseline support state;
- `contract_supported`;
- one required disposition;
- support evidence refs;
- one authorization per allowed boundary code;
- observable world refs and maximum claim ceiling for each boundary.

The validator rejects a supported baseline that silently chooses a boundary, an unknown boundary
fact, a missing authorization or an executable disposition without a valid design family.

### E0-C3. Time and interaction replay

All 45 Episodes retain a typed EvaluationClock. Message injection now uses user-observable triggers:
initial、after clarification、after visible measurement proposal、while investigation is pending and
after visible provisional answer. Runtime node/effect names no longer control Episode interaction.

### E0-C4. Agent/Evaluator projection boundary

The compiler uses two independent whitelists. The initial Agent view exposes only injected messages,
provided context, clock and governed inspection refs. Evaluator truth, future messages, title,
stakes, outcome envelope, forbidden outcomes, siblings and grader authority remain in the oracle
view. Agent projection also excludes Episode/core/profile lookup handles. Canary tests cover direct
and inspection-surface leakage; the CLI only exposes the read-only contract check. Process and
credential isolation remains G3.2 work.

### E0-C5. External evaluation authority

The following artifacts now have strict schemas and canonical hashes:

- canonical coverage taxonomy;
- Source、Review、Corpus and Grader registries;
- per-Episode authority conformance bindings;
- cross-Gate world profiles;
- independent-review packages;
- Promotion、held-out and run manifests;
- grader-calibration package;
- derived E0 readiness manifest.

`verify_gate3_e0.py` reads the canonical artifact set and never accepts a caller-selected catalog as
Gate authority. Repository-local receipts and caller-controlled environment variables cannot
establish trust. The GitHub/Sigstore request, workflow and verification contracts are implemented;
protected state, a real bundle and the trusted canonical connector remain unprovisioned. The local
verifier rejects all configured roots and keeps `external_admission_verified=blocked`. Final manifests additionally require a
schema-valid, same-root, exact-epoch predecessor chain whose complete history is externally
authorized.

### E0-C5a. Measurement-gold self-assertion

Three extra authority checks close the remaining gold-label bypasses:

- an Episode claim ceiling bounds every estimand-specific target; each boundary authorization binds
  explicit claim-target IDs and applies the same lattice target by target;
- an identifiable truth fact may cite only refs present on the exact AgentWorldView surface;
  inaccessible or missing contracts and unprojected conditions do not qualify, and world refs are
  globally unique;
- an executable counterfactual is replayed against the Episode through its JSON Pointer and its
  materialized authority digest is recomputed. The mutation dimension constrains legal authority
  leaf paths, scalar replacement keeps the intervention atomic, and the materialized Episode must pass
  the complete schema and semantic validator. Null placeholders, stale `before`, no-op mutations,
  control-metadata mutations, invalid sibling states and arbitrary digests fail closed.

### E0-C6. Three-layer verdict

`EvaluationRunResult` requires product behavior、authority conformance and implementation results.
It binds a frozen run cell、Episode/world/authority profile hashes、the exact required check set and
a runner-verified artifact index. Final verdict is derived by strict AND. Critical veto、oracle
leakage、missing artifact、blocked or invalid layer prevents pass.

## 3. Current machine-derived blockers

The readiness manifest currently reports:

- expert source claim: 8 Episodes pending external author identity/evidence;
- historical source claim: 6 Episodes pending incident/eval records;
- trusted source-authority roots: `0`; each pool requires at least two independent sources;
- independently double-reviewed Episodes: `0`; policy floor gap: `36`;
- truth facts awaiting identifiability review: `84`;
- Episodes missing explicit estimand/per-claim structure: `45`;
- counterfactuals lacking executable materialization: `147`;
- reviewed coverage: empty until promotion;
- grader calibration: pending;
- held-out manifest: unsealed, zero entries;
- promotion manifest: draft, zero entries;
- run manifest: draft.

These are business/evaluation authority gaps. Policy-pinned authority roots are empty, so adding
principals to a registry cannot create trust. Lowering floors, self-registering fictional experts or
using subagent output as human attestation is forbidden.

### GitHub activation snapshot

The public remote is live at `https://github.com/mymorphstyle-dotcom/waje-bi-v2`; `main` and
`codex/bi-agent-vnext` have been pushed.

Current remote controls:

- `main` requires a pull request, dismisses stale reviews, enforces administrators, requires linear
  history and conversation resolution, and disables force-push/delete;
- the current development account has no second collaborator, so approving-review count is `0` and
  CODEOWNER review is not yet required;
- `gate3-admission` exists and allows only protected branches;
- the environment currently has no required reviewer, reports `can_admins_bypass=true`, and has no
  approved admission-authority secrets;
- required status checks will be configured after the first `vNext validation` check exists on
  GitHub.

This is sufficient to exercise ordinary public CI after merge. It does not satisfy independent
change control or external admission authority. The machine-derived Gate keeps this state blocked.

The external admission decision is accepted: public GitHub Actions workload identity signs the exact
admission request through GitHub Artifact Attestations/Sigstore. The public remote is
`mymorphstyle-dotcom/waje-bi-v2`, repository ID `1317104320`, owner ID `278493004`. The repository
defines strict request/provider-state schemas and verifies repository/commit/ref/workflow/run
identity. A single externally approved admission-authority digest covers policy, authority-root
bundle, verifier release, evaluated artifacts, the candidate-measured runtime payload and exact
authorized Source/Review and manifest hashes. No long-lived signing key enters a runner. The
candidate measurement is hash-bound; a digest-pinned hermetic builder and complete runtime closure
remain activation blockers.

Operational provisioning remains open: merge the protected workflow, configure the
`gate3-admission` environment and its provisional protected secrets, issue the first real bundle,
then add an atomic admission/provider-state predecessor CAS and a trusted provider-state
reader/connector into canonical readiness.

### E0-C5b. Protected CI admission implementation

The accepted design is recorded in
`docs/adr/2026-07-30-gate3-protected-ci-admission.md`. The current implementation adds:

- public GitHub remote with immutable numeric repository/owner identity;
- `github-admission-request.schema.json` and `github-provider-state.schema.json`;
- provider-neutral canonical hashing and verified authority values in
  `gate3_admission_authority.py`;
- `github_gate3_admission.py` for strict provider state, complete admission hash, dual predecessor
  provenance, pinned `gh` executable, immutable input snapshots and Sigstore subject verification;
- `build_gate3_github_admission_request.py` for Python 3.12 executable, dependency inventory,
  critical import and evaluated source-tree attestation;
- verifier-release coverage for Python, Node package/lock, uv lock, isolation policy, GitHub
  workflows, CODEOWNERS, schemas and verifier code;
- a read-only PR/merge-queue workflow and a push-to-main admission workflow with an exact
  `{candidate, attest}` job set;
- full-SHA action pinning, no checkout or repository-code execution in the privileged job;
- provider/workflow attack tests covering identity drift, trust/release rollback, predecessor and
  operation mismatch, subject substitution, duplicate JSON keys, local environment injection,
  non-GitHub-hosted certificates, second signer jobs, symlinks and untrusted workflow triggers.

The canonical Gate still accepts no caller-selected provider state, bundle, context, clock or
verified object. Source, independent review, measurement gold, counterfactual, calibration,
held-out, promotion and run-manifest conditions remain independent strict-AND gates.

## 4. Review package

`vnext/evals/gate3/promotion/review-packages.json` contains 45 hash-bound packages. Each package
provides:

- exact Episode core hash and authoring reference;
- source record;
- frozen EvaluationClock;
- Agent/Evaluator view hashes;
- business review scopes;
- measurement review scopes;
- machine-detected open findings;
- required independent reviewer roles.

Any Episode change invalidates the package hash and requires a new review.

## 5. G3.1 entry rule

G3.1 can start only when the read-only policy-ready command and the hard entry command
`npm run gate3:enter:g3.1` both return zero, and the derived readiness manifest says:

```text
derived_status = ready
entry_decision = allow_g3_1
```

Current result remains:

```text
derived_status = blocked
entry_decision = deny_g3_1
```

## 6. Verification evidence

- `npm run test:bootstrap`: 127 tests passed, 7 environment-dependent tests skipped;
- Gate 3 trust/attack suite: 64 tests passed;
- `npm run check:contracts`: passed;
- `npm run check:evals:gate3`: structural integrity passed with honest blocked readiness;
- `actionlint v1.7.12`: both GitHub workflows passed;
- `npm run check`: clean-copy build/test/health passed under Python 3.12.13;
- `npm run check:evals:gate3:policy-ready`: exit 1 as required;
- `npm run gate3:enter:g3.1`: exit 1 with `entry_decision=deny_g3_1`.

The clean-copy audit also verifies that Gate 3 source evidence is owned under `vnext/`. The only
root-level deployment projection is the policy-listed `.github/` file set; clean-copy validation
copies and exact-hash verifies it without any historical implementation directory.

## 7. Adversarial closure

The combined review first reproduced:

- process-local verified-object forgery;
- caller-selected trust policy and historical clock injection;
- a missing canonical protected-runner entry;
- movable workflow tags and missing run-attempt identity;
- incomplete Python/dependency/runtime binding;
- file mode bits being mistaken for protected control-plane provenance;
- trust-policy triplet rollback without an external monotonic anchor.
- self-authorizing policy/verifier changes inside a protected job;
- mutable repository names without numeric IDs;
- fork and privileged-event entry paths;
- repository code executing in the OIDC/attestation job;
- artifact subject substitution and JSON duplicate-key ambiguity.
- candidate-controlled runtime and authorization fields outside the externally approved hash;
- provider-mandated root `.github/` files missing from Day 0 deletion-independence proof.

The repository fixes the implementable contract findings, selects GitHub/Sigstore, and removes the
superseded raw Ed25519 profile. Remote environment protection, first-bundle verification, a trusted
canonical connector, exact-SHA required-check publication, atomic provider-state/admission CAS and
a digest-pinned hermetic builder remain explicit activation blockers.
