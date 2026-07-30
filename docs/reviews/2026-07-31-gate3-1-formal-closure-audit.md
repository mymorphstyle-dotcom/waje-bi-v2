# Gate 3.1 Formal Closure Audit

## Verdict

Date: 2026-07-31

`G3.1 local implementation = complete`.

`G3.E0 formal admission = blocked`.

`entry_decision = deny_g3_1`.

The remaining blockers are protected human and external-control-plane
decisions. Repository authoring, case-file materialization and deterministic
validation cannot sign those decisions on their own.

## Machine-complete scope

- 36 WAJEgame Required Episodes across launch question, business chain,
  measurement regression and authority stress;
- 8 decision goals, 10 measurement challenges, 7 temporal shapes, 7 data
  conditions, 7 conversation dynamics and 6 risk types with no authoring
  coverage gap;
- 54 truth facts retained as `pending_independent_review`;
- explicit estimand-to-claim and claim-to-case coverage for every Episode;
- 120/120 counterfactuals with executable atomic patches, claim-local effects,
  replacement expectations and replayed materialized digests;
- 41 case-file authorities and 52 hash-bound materialization records;
- four real snapshot receipts, controlled WAJEgame fixtures and the USER008
  append-only prior-authority repair case;
- Agent/Evaluator projection isolation, three-layer strict-AND result contract,
  immutable runner-artifact binding and fail-closed settlement checks;
- canonical GitHub provider connector, trusted-root/freshness validation,
  replay rejection and monotonic provider-state/admission CAS contract;
- deterministic corpus, ledger, profiles, review packages and readiness
  regeneration;
- DeepSeek role candidates frozen for calibration:
  - Primary Agent: `deepseek-v4-pro`, thinking enabled;
  - runtime Reviewer: `deepseek-v4-pro`, thinking disabled;
  - evaluation Reviewer: `deepseek-v4-flash`, thinking enabled.

The model assignments remain `quality_probe_only`. Formal calibration must
promote them; the probe cannot promote itself.

## Adversarial findings closed

1. A substantive user correction could supersede every base claim and leave no
   replacement target. Scope, metric and decision changes now require a
   hash-bound replacement expectation. New semantic targets use separately
   authored `variant_authored_gold`.
2. The replacement compiler used to overwrite or reject valid authored
   variant gold. It now preserves and validates valid authored gold, and only
   derives replacements from retained base claims when that derivation is
   legitimate.
3. A counterfactual-only source could avoid case-file readiness. Every
   physically reachable authority now enters materialization and independent
   review readiness.
4. A JSON file with a matching hash could masquerade as a case file. Readiness
   now requires the artifact contract implied by source mode, plus matching
   authority identity, evaluation clock, source identity and exposure/query
   references.
5. Old tests still expected planned fixtures and incomplete measurement gold.
   Those assertions were rewritten to the current no-backcompat contract.
6. Open date semantics remain an Agent decision. Tests accept defensible
   window choices while requiring the accepted Frame to carry actual calendar
   ranges, month offsets, calendar/observed/valid exposure, timezone, business
   day, release coverage and sensitivity boundaries.

## Verification evidence

- `npm run check:evals:gate3`: passed;
- `npm run check:eval-corpus:gate3`: passed;
- `npm run check:eval-views:gate3`: passed;
- `npm run check:contracts`: passed;
- clean-copy isolation: passed;
- clean-copy Python: `3.12.13`;
- clean-copy test suite: 222 tests, 0 failures, 8 environment-bound skips;
- clean-copy wheel: built independently with `Requires-Python: >=3.12`;
- generated coverage ledger: `missing_coverage={}` and
  `counterfactual_role_gaps={}`;
- case-file readiness: 0 missing authorities, 0 pending materializations and
  0 integrity gaps.

## Formal blockers

1. Forty-one case-file authorities need content-bound approval from two
   distinct people: a business owner and a measurement reviewer.
2. Thirty-six Episode review packages need the same independent role coverage.
3. Fifty-four truth facts need identifiability/support decisions tied to the
   reviewed Episode hashes.
4. Historical and suite-rebase source provenance still needs protected
   external evidence; verified sources have not yet received external
   authorization.
5. A dedicated human calibration reviewer must label the frozen calibration
   sample; grader thresholds cannot be promoted from the model probe.
6. Promotion, protected held-out sealing and the complete base-plus-sibling run
   manifest must follow those reviews.
7. GitHub `gate3-admission` needs independent protected approval, a trusted
   workflow revision, a real Sigstore bundle and a trusted provider-state
   record. The current repository has no independent required reviewer and
   allows administrator bypass, so local readiness correctly refuses entry.

## Next authority commit

The next commit is a human assignment, followed by review:

1. assign one business owner and one measurement reviewer with distinct stable
   principal IDs and distinct GitHub accounts;
2. authorize those principals through the protected GitHub environment;
3. review source provenance, 41 case files, 36 Episode packages and 54 truth
   facts against their current hashes;
4. regenerate readiness; any content change invalidates the old review and
   returns only the affected item to review;
5. continue to calibration, held-out sealing, promotion, run freeze and the
   signed external admission.

Until those steps are complete, `npm run gate3:enter:g3.1` must return nonzero.
