# Gate 3 WAJEgame Episode Suite Rebase

Status: confirmed for implementation on 2026-07-31.

## 1. Decision

Gate 3 launch acceptance is grounded in WAJEgame business analysis.

- The required corpus contains 36 WAJEgame Episodes.
- Up to four cross-domain transfer probes live in a separate research lane and
  have no Gate authority.
- Source pools record provenance. They do not impose per-pool Episode quotas.
- Current real ClickHouse, frozen WAJEgame synthetic worlds, and known
  missing-contract worlds are distinguished explicitly.
- Superseded cross-industry fixtures, hashes, review packages, policy pins, and
  compatibility assertions are replaced. There are no production consumers or
  backward-compatibility obligations.

The rebase corrects an authoring drift in the previous policy. A fixed quota of
six Episodes for each of five source pools encouraged cross-industry filler.
That made source provenance determine business coverage. The new policy derives
coverage from WAJEgame question families, factor groups, measurement risks, and
authority-continuity risks.

## 2. Required suite

### 2.1 Launch questions: 8

The eight user-provided paid-amount questions remain verbatim seeds:

1. paid-amount change explanation;
2. recurring pattern explanation;
3. activity, acquisition, release, payment, holiday, and external-event review;
4. revenue-health and concentration-risk review;
5. segment and factor attribution;
6. anomaly and black-swan review;
7. multi-baseline comparison;
8. data-quality and evidence-sufficiency review.

### 2.2 WAJEgame business chain: 10

The business-chain group covers:

1. recharge-page and payment-funnel change;
2. acquisition mix, registration, first payment, and paid conversion;
3. payment terminal outcomes and release maturity;
4. payer retention and reactivation;
5. paid amount versus gameplay profit/GGR health;
6. betting and GGR reconciliation;
7. first-paid-user metric identity;
8. reactivation-campaign value and causal boundary;
9. acquisition-channel ROAS and attribution boundary;
10. active-user, paid-active, and gameplay-active definition boundaries.

### 2.3 Measurement and evidence regression: 10

The regression group covers:

1. target-month start versus previous-month end;
2. 28/29/30/31-day and cross-year calendar resolution;
3. MTD versus complete-period eligibility;
4. raw total versus exposure-normalized intensity;
5. official new-user-base ratios versus an unavailable user-level funnel;
6. interaction and unexplained residual in GGR decomposition;
7. observational association versus causal ceiling;
8. paid amount, GGR, and profit semantic-contract conflict;
9. Africa/Lagos business day versus source/UTC date;
10. user/channel mix reversal and Simpson-type aggregation error.

### 2.4 Authority and conversation stress: 8

The stress group combines WAJEgame questions with durable runtime events:

1. vague, multi-estimand paid-amount questions;
2. Reviewer veto of an immature high-value-payer claim and loop re-entry;
3. cohort/channel correction followed by a stale effect completion;
4. product-scope expansion followed by an old-scope result;
5. Reviewer veto of activity-window uplift as causal impact;
6. Reviewer veto of targeted reactivation as randomized evidence;
7. requests for prediction, user ranking, and automatic incentives outside the
   current product boundary, including worker restart recovery;
8. honest stop on a missing payment-failure contract and same-case resume after
   a governed contract/release event.

## 3. Candidate mapping

| Group | Required Episode IDs |
|---|---|
| launch questions | `G3-USER-001..008` |
| business chain | `G3-ADV-002`, `G3-GF-005`, `G3-GF-006`, `G3-GF-007`, `G3-EXP-001`, `G3-EXP-005`, `G3-EXP-008`, `G3-EXP-010`, `G3-EXP-012`, `G3-ROOT-001` |
| measurement regression | `G3-GF-001`, `G3-GF-002`, `G3-GF-003`, `G3-GF-004`, `G3-GF-008`, `G3-GF-009`, `G3-GF-010`, `G3-GF-011`, `G3-GF-012`, `G3-EXP-002` |
| authority stress | `G3-ADV-001`, `G3-ADV-004`, `G3-ADV-005`, `G3-ADV-006`, `G3-ADV-007`, `G3-ADV-008`, `G3-ADV-009`, `G3-EXP-007` |

The separate non-gating transfer lane initially retains:

- `G3-EXP-003`: SaaS account retention;
- `G3-EXP-004`: loan application funnel;
- `G3-EXP-006`: delivery timeliness;
- `G3-EXP-011`: inventory-constrained demand.

The following duplicate or off-scope candidates are retired:

- `G3-ADV-003`;
- `G3-ADV-010`;
- `G3-ADV-011`;
- `G3-ADV-012`;
- `G3-EXP-009`.

Retirement removes them from the Gate catalog and all derived authority
artifacts. No compatibility alias is retained.

## 4. Claim-scoped case-file binding

Every required Episode carries a `suite_binding` with:

- `business_domain = wajegame`;
- one primary coverage group;
- WAJEgame factor-group references;
- launch question-family references;
- claim-independent business coverage metadata.

Evidence availability lives in `data_source_bindings`. A single Episode may
combine:

- `frozen_real_snapshot`;
- `controlled_synthetic_fixture`;
- `known_contract_gap`.

Each claim target has its own claim case: source uses, data-contract state,
business-evidence state, permitted resolution, effective claim ceiling,
evaluation turn, Agent-observable prerequisites, oracle truth references,
boundary codes and reversal conditions. A claim cannot use a source or
contract release before that turn. An Episode-level status is derived from
these claim cases. A local gap cannot cancel an unaffected claim.

Every real or synthetic binding in a completed claim case resolves to a
versioned case-file authority. The authority pins the evaluation clock,
dataset or fixture identity, physical artifact digest, allowed claim scope and
independent-review state. Promotion requires one content-bound approval from
each required role, with distinct reviewer principals. Any authority-content
or materialized-artifact change invalidates those approvals.
Known gaps resolve to the contract backlog. `data_source_bindings` are part of
the immutable Episode core hash.

Each executable counterfactual is one semantic intervention with an atomic
patch set. A single intervention that changes world description, semantic
contract and physical source binding declares `composite_authority`; each
patched object retains its own identity. If a new scope keeps some claims and
drops others, the sibling records claim-local `recompute` and
`supersede_or_omit` effects under a `mixed` aggregate summary. A replacement
source used only by a counterfactual still enters case-file materialization,
content-bound double review and the frozen run matrix.

Authority-stress Episodes may also contain scheduled business-visible events:
Reviewer objections, effect completions, duplicate deliveries, worker restarts,
and contract releases. The Agent sees only events whose user turn has occurred.
The evaluator retains the expected authority disposition.

## 5. Policy rebase

Gate 3 evaluation policy v4 replaces source-pool quotas with:

- exactly 36 required catalog Episodes;
- coverage-group floors of 8, 10, 10, and 8;
- `business_domain = wajegame` for every required Episode;
- at least 12 multi-turn Episodes;
- at least six critical-risk Episodes;
- at least three counterfactual siblings per Episode;
- complete coverage of the canonical decision, measurement, time, data,
  conversation, and risk taxonomy;
- complete suite-level coverage of all 14 WAJEgame factor groups, all eight
  launch question families, and all three source modes;
- verified source provenance for promoted Episodes without a per-source-pool
  count target.

The transfer lane is stored outside the Gate candidate directory. Gate
generators, promotion, review-package counts, held-out manifests, run
manifests, readiness, and admission hashes do not read it.

## 6. Rewrite rules

- Domain adaptation changes the business world, contracts, data conditions,
  truth facts, decision stakes, outcomes, and counterfactuals together. Simple
  noun replacement is rejected.
- Current ClickHouse Episodes use only accepted dataset, timezone, currency,
  release, grain, privacy, and provenance contracts.
- Frozen synthetic Episodes use WAJEgame entities and formulas from the factor
  SSOT, while keeping their data independent of current implementation output.
- Missing-contract Episodes make honest boundary handling the expected
  disposition. They do not invent proxy data.
- The eight user questions retain their original wording. Added worlds and
  expectations remain candidate authoring until independent business and
  measurement review.
- Open business semantics remain typed-LLM decisions. Deterministic checks
  enforce structure, authority identity, data contracts, evidence boundaries,
  and publication safety.

## 7. Acceptance

The rebase is complete when:

1. only the four WAJEgame candidate files feed the Gate catalog;
2. the required catalog contains 36 unique Episodes with the expected group
   counts;
3. every required Episode binds at least one WAJEgame factor group and one
   question family, and the complete suite covers every required reference;
4. no required Episode contains a non-WAJE business world;
5. transfer probes are unreachable from every Gate generator and verifier;
6. policy, schema, generator, catalog validator, readiness verifier, manifests,
   profiles, review packages, hashes, and documentation agree on the new suite;
7. structural checks pass while promotion remains honestly blocked on external
   source/review/calibration/held-out/admission requirements;
8. an adversarial review finds no source-quota fallback or retired-ID
   compatibility path;
9. completed claim cases exactly cover their claim targets, and every required
   observation is reachable through the Agent view;
10. every launch counterfactual has an exact replayable atomic patch set and enters
    independent semantic review; each affected claim binds the base claim-case
    digest and expected authority effects, every unaffected claim is explicit,
    and replayability alone does not make it gold;
11. case-file authorities, controlled fixtures and real result oracles are
    materialized and independently reviewed before formal admission;
12. a frozen run contains the base case and every sibling for each promoted
    Episode, while every result binds the exact variant and sibling digest.

## 8. Current implementation state

As of 2026-07-31:

- Episode and policy contracts are on v4;
- all 36 Required Episodes contain complete estimand and claim-scoped case
  authoring;
- all 120 counterfactual mutations are exact, schema-valid and replayable;
- every sibling has claim-scoped authority effects, replacement expectations
  and a replayed materialized digest;
- Agent projections expose semantic gaps, data probes and prior-authority
  lookups without exposing evaluator truth; verified, planned and known-gap
  sources are reported distinctly; future releases, future truth support and
  source bindings with a later availability turn remain hidden until their
  own turn;
- 41 real-snapshot or controlled-fixture authorities are registered with 52
  hash-bound materialization records;
- counterfactual-only replacement authorities enter the same materialization
  and independent-review readiness checks as base authorities;
- `FIXTURE-WAJE-RUN-EVIDENCE-REPAIR-V1` supplies USER-008's prior authority
  graph, a 19-milestone append-only repair schedule, 19 observation records
  and eight claim-local dispositions;
- all verdicts bind the frozen run manifest, exact grader registry and runner
  artifact index; critical vetoes bind failed registered checks, and
  calibration labels cross-bind an externally attested human review, exact
  result and runner index;
- the Python 3.12.13 clean-copy suite passes 222 tests with eight
  environment-bound skips;
- formal admission remains `deny_g3_1` while all case-file authorities stay
  in `authoring`, 54 truth facts need independent review, and protected
  source/review/calibration/held-out/promotion/run/admission work is open.
