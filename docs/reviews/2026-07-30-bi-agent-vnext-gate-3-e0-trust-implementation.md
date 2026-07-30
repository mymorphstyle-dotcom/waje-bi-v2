# WAJE BI Agent vNext Gate 3 E0 trust foundation

## 1. Gate verdict

| 项目 | 状态 |
|---|---|
| Authoring contract | Complete for E0 infrastructure |
| Source/Review authority | Implemented; real records incomplete |
| Agent/Evaluator projection contract | Complete; runtime process isolation belongs to G3.2 |
| Grader/Authority profile contract | Complete |
| Three-layer result contract | Complete |
| Independent review and calibration | Blocked |
| G3.1 entry | `deny_g3_1` |
| Entry interview | 本轮无需用户决策；真实 reviewer 身份和来源证明出现前不伪造签字 |

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
establish trust. Until an external issuer is selected and implemented, the local verifier rejects
all configured roots and keeps `external_admission_verified=blocked`. Final manifests additionally
require a schema-valid, same-root, exact-epoch predecessor chain whose complete history is externally
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

External admission remains one architectural decision. The recommended default is a protected CI
identity that signs a canonical admission envelope binding verifier release, authority-root bundle,
Source/Review record hashes and manifest hashes. A dedicated admission service is the alternative
when online revocation or multi-team issuance is required. The repository provides no local
fallback.

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

- `npm run test:bootstrap`: 106 tests passed, 7 environment-dependent tests skipped;
- Gate 3 trust/attack suite: 43 tests passed;
- `npm run check:contracts`: passed;
- `npm run check:evals:gate3`: structural integrity passed with honest blocked readiness;
- `npm run check`: clean-copy build/test/health passed under Python 3.12.13;
- `npm run check:evals:gate3:policy-ready`: exit 1 as required;
- `npm run gate3:enter:g3.1`: exit 1 with `entry_decision=deny_g3_1`.

The clean-copy audit also verifies that Gate 3 source evidence is owned under `vnext/`; deleting
repository-external documentation does not change the readiness result.
