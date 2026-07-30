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

- 45 schema-valid authoring Episodes;
- 8 real-user paid-amount questions from one source task;
- 21 multi-turn Episodes;
- 41 high/critical-risk Episodes;
- 147 structured counterfactual mutations;
- 45 independent-review packages;
- G3.E0 remains `blocked`; `entry_decision=deny_g3_1`.

The blocked state is intentional. Expert/historical provenance, independent double review, truth
identifiability, per-claim ceilings, grader calibration, a sealed held-out set, promotion and a
frozen run manifest remain incomplete.

## Protected trust boundary

Checked-in JSON and caller-controlled environment variables cannot authorize themselves. The local
verifier therefore rejects every configured authority root and emits
`external_admission_verified=blocked`. A local receipt file, registry edit, invented reviewer or
locally generated predecessor chain cannot unlock readiness.

The remaining architecture decision is the external issuer boundary: a protected CI identity with
a signed admission envelope, or a dedicated admission service. Once selected, the adapter must
verify issuer identity outside repository control and return exact authorized Source/Review and
manifest hashes. Every manifest transition will continue to bind the same root, exact prior epoch,
canonical predecessor hash, monotonic status transition and recursively authorized history.

## Authority layout

```text
gate3/
├── evaluation-episode.schema.json
├── evaluation-views.schema.json
├── evaluation-run-result.schema.json
├── gate3-e0-trust.schema.json
├── gate3-eval-policy.json
├── taxonomy/
│   └── coverage-taxonomy.json
├── candidates/
├── catalog/
│   └── gate3-authoring-candidates.json
├── registries/
│   ├── source-registry.json
│   ├── review-registry.json
│   ├── corpus-registry.json
│   └── grader-registry.json
├── profiles/
│   ├── authority-conformance-profiles.json
│   └── cross-gate-world-profiles.json
├── promotion/
│   └── review-packages.json
├── calibration/
│   └── grader-calibration-package.json
├── manifests/
│   ├── promotion-manifest.json
│   ├── protected-held-out-manifest.json
│   └── run-manifest.json
├── coverage-ledger.json
└── gate3-e0-readiness.json
```

`candidates/` contains human/agent authoring inputs and is read-only to the corpus generator. The
exact-union catalog and registry/profile/review-package artifacts are deterministic outputs.
Review, promotion, calibration, held-out and run manifests are authority-owned inputs; generation
commands cannot reset them.

## Agent and evaluator isolation

`compile_gate3_eval_views.py --check` validates both projections from explicit whitelists:

- `AgentWorldView` receives the current injected messages, frozen clock, provided context and
  governed semantic/data inspection references;
- `EvaluatorOracleView` receives the complete message plan, truth facts, support expectation,
  outcome envelope, forbidden outcomes and grader/profile references.

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

The final verdict is derived by strict AND. A critical veto, missing artifact, blocked layer,
invalid layer or oracle leakage cannot be averaged away.

For measurement gold, an Episode-level claim ceiling is the maximum for every claim target. A truth
fact marked identifiable must cite world facts the Agent can discover and access. An executable
counterfactual must carry one semantic intervention, exact non-null before/after values where
applicable, and a digest recomputed by replaying its JSON Pointer mutation. Textual assertions and
arbitrary sibling hashes remain unexecutable.

## Lifecycle

1. Author candidate Episodes and business worlds.
2. Normalize and hash immutable Episode cores.
3. Resolve a verified Source Registry record.
4. Compile Agent/Evaluator views and cross-Gate profiles.
5. Obtain independent business-owner and measurement-reviewer records against the same core hash.
6. Promote reviewed Episodes through the PromotionManifest.
7. Calibrate graders using human labels.
8. Seal an external held-out manifest without checked-in plaintext.
9. Freeze the run manifest.
10. Derive the read-only readiness manifest and allow G3.1 only when every condition passes.

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
```

The three generation commands are explicit authoring actions. Both checks are read-only.
`check:evals:gate3` verifies structural integrity while preserving honest blocked conditions.
`check:evals:gate3:policy-ready` and `gate3:enter:g3.1` currently exit nonzero. Every future G3.1
entrypoint must depend on the latter hard gate.
