# Gate 3 Launch Episodes Post-confirmation Adversarial Audit

Date: 2026-07-31
Scope: `G3-USER-001..008`, Episode v4, Agent/Evaluator views, product
grader expectations, authority conformance profile, current WAJEgame data and
contract bindings.
Review mode: primary review plus three independent adversarial reviews
(measurement/grader, WAJEgame business/data, authority/runtime).

## 1. Verdict

The eight launch Episodes preserve the team's intended business questions,
estimands, evidence ceilings and local degradation principles. The findings
below record the v3 state that triggered the repair. The v4 structural repair
is complete; physical case-file materialization and independent admission
review remain open.

All three independent reviews found the same three structural blockers in v3:

1. the Episode-level data realm can claim current ClickHouse support when the
   evaluation clock, contract refs or hidden facts are outside the real release;
2. the Agent cannot observe most facts needed to form the expected typed
   boundaries;
3. a single Episode-level disposition cannot represent a mixed answer in which
   supported claims continue and unsupported claims degrade locally.

These are test-authority defects. Leaving them in place would reward early
stopping, guessing hidden gaps, or accepting synthetic facts under current-data
provenance.

Formal Gate status remains `deny_g3_1`.

### 1.1 Resolution recorded after team confirmation

- Episode-level data realms were removed. Claim cases now bind
  `frozen_real_snapshot`, `controlled_synthetic_fixture` and
  `known_contract_gap` independently.
- `G3-USER-001..008` exactly cover every claim target with source uses,
  support state, disposition, applicability, effective ceiling, required
  observations, boundary codes and reversal conditions.
- Agent projection now exposes semantically discoverable conditions, contract
  gaps, data probes and authority lookups.
- The shared real snapshot uses target business date `2026-06-02`,
  as-of `2026-06-03T10:00:00+01:00`, Africa/Lagos `[00:00, 24:00)`.
- All 24 launch mutations have exact before/after values and verified replay
  digests. This verification proves mechanical replay; independent business
  and measurement review still decides semantic validity.
- `data_source_bindings` now participate in the Episode core hash.
- Case-file authority resolution, source-mode agreement, dataset/fixture
  scope, evaluation-clock agreement, claim scope and Agent-observation reach
  are hard-validated.
- USER-008 is bound to a controlled prior-authority fixture; its complete
  object graph is part of the authority observation package.
- Readiness has a dedicated `claim_case_files_ready` condition and remains
  blocked while authorities are in authoring and physical bindings are
  planned.

## 2. Blocking findings

### 2.1 Data-world provenance and evaluation clocks do not agree

All eight Episodes currently use an as-of instant of 2026-07-30, so “yesterday”
means 2026-07-29. Current accepted coverage ends earlier:

- paid-order detail: 2026-07-04;
- payment final outcome: 2026-06-02;
- market dashboard and cross-source analytical releases: 2026-06-02.

`current_clickhouse` and `hybrid_current_and_missing` Episodes therefore cannot
produce Evidence for the authored target day. Several Episode-local contract
refs also have no repository authority, including invented health, anomaly,
quality, rollout and event-timeline contracts.

Some hidden worlds require conditions that the current intake explicitly does
not establish, such as late status backfill, an observed two-hour payment
incident, stable experiment assignment, refund-adjusted growth and canonical
cross-account payer identity.

Required correction:

- every evaluable fact must bind an exact real snapshot/query result, a declared
  synthetic fixture, or an explicit missing-contract boundary;
- current ClickHouse provenance may only be used inside the accepted release
  window and with repository-resolvable contracts;
- synthetic conditions must never inherit current-data provenance.

### 2.2 Expected boundary facts are invisible to the Agent

The view compiler currently exposes:

- only `available + accessible` contracts;
- only `provided_to_agent` or `discoverable_by_data_probe` data conditions.

It hides partial, missing, stale or denied contract status and hides data
conditions marked `discoverable_by_semantic_inspection`. Across USER-002..008,
22 of 25 boundary authorizations contain at least one required observation that
the Agent cannot reach. USER-006 additionally uses an evaluator-only large-payer
fact as a required Agent observation.

Required correction:

- semantic inspection must expose contract identity, state, version and access
  boundary without exposing protected data;
- semantically discoverable conditions must appear on an Agent-callable
  inspection surface;
- validation must enforce
  `required_observation_refs ⊆ agent_accessible_world_refs`;
- evaluator-only facts cannot authorize expected Agent behavior.

### 2.3 Global disposition rewards whole-question refusal

USER-002..007 currently set `required_disposition=typed_boundary`, even though
each Episode contains both executable and blocked claims. Examples:

- daily/month-position patterns can continue while intraday/gameplay claims
  remain bounded;
- version rollout evidence and outage attempts can continue while overlapping
  activity/budget effects remain unidentified;
- supported channel, geography, device and payment-method bridges can continue
  while package/gameplay attribution remains bounded;
- three raw baselines can continue while an activity-adjusted normal range
  remains provisional.

Required correction:

- support and disposition move to each claim target;
- the Episode declares which claims must continue, may remain provisional, must
  stop at a typed boundary, or must be omitted;
- any unrelated obligation cancelled by a local gap is a blocking failure;
- Episode completion summarizes claim outcomes and does not replace them.

### 2.4 Counterfactual siblings are descriptive placeholders

All 24 launch sibling mutations lack executable before/after values,
materialized digests and verified execution state. Several measurement-changing
siblings mutate evaluator-only fields, so the Agent receives the same question
while the grader expects a different measurement identity.

Required correction:

- intent changes enter through Agent-visible user messages or business events;
- each mutation is atomic and materialized;
- pairwise grading compares actual traces;
- meaning-preserving, measurement-changing and boundary-changing labels are
  independently reviewed.

### 2.5 USER-008 has no prior conclusion to audit

The user says “这个结论”, but AgentWorld contains no prior Answer, claim,
Frame, Evidence, query/result or snapshot/release identity. The current
expectation also mixes product behavior with internal artifact naming.

Required correction:

- inject a persisted prior-authority fixture with at least a total-amount claim
  and a channel-driver claim;
- the product grader checks replayable provenance, local evidence verdicts and
  repair obligations;
- artifact IDs, immutable validity records, supersession and projection joins
  are checked by authority conformance;
- missing prior provenance remains an honest clarification or
  unverifiable boundary.

### 2.6 Authority checks are abstract and cannot inspect a trace

The shared authority profile lists five invariant names, while no typed
authority observation bundle defines the required joins among accepted
Question/Frame/Plan heads, logical execution, Evidence admission and validity,
AnswerVersion and Workflow projection.

Required correction:

- define a machine-verifiable authority observation bundle with object IDs,
  head epochs/hashes, causation, supersedes relations and compatibility proof;
- distinguish Episode admission reviewers from runtime Reviewer objections;
- schedule runtime events by durable milestones, including frame candidate,
  effect receipt, evidence admission and answer proposal;
- keep Gate 3 publication expectations at provisional/hard-deny; claim-scoped
  settlement oracles belong to Gate 5.

### 2.7 Several claim scopes still require local repair

- USER-001 and USER-005 select a prior-day primary baseline that the user did
  not explicitly provide. It requires an accepted business default or an
  Agent-proposed recorded inference with a correction path.
- USER-003 needs a joint activity-plus-budget exposure estimand; two perfectly
  overlapping events cannot have separately identified effects.
- USER-003 cannot infer causal creative/version effects from assignment records
  alone; identification assumptions and a local downgrade path are required.
- USER-005 gameplay gaps must not lower the independent behavior-factor bridge.
- USER-006 hourly gaps must not lower complete-day claims.
- USER-007 driver claims must be separated by baseline so one contaminated
  baseline does not degrade all comparisons.
- USER-008 quality boundaries must attach only to claims that consume the
  affected status, mapping or provenance.

### 2.8 Reviewer gold is still pending

All truth facts remain `pending_independent_review`. Claim targets have ceilings
but lack reviewed current-world support states, minimum evidence and reversal
conditions. The current files remain authoring candidates and cannot serve as
gold until source authority, business review, measurement review,
counterfactual execution and calibration are complete.

## 3. Direct correction completed during this audit

Two Required Episodes still carried a 04:00 business-day residue, and one
contradicted itself by also requiring a 00:00 cutoff. Both were aligned to the
confirmed Africa/Lagos local calendar day `[00:00, 24:00)`. The time-zone
regression still tests local-date versus UTC-date direction reversal.

## 4. Recommended evaluation-world model

The current `data_realm.kind` combines two independent questions:

1. where a fact came from;
2. whether a claim is currently supported.

This produces false all-or-nothing states. The recommended model binds every
evidence obligation to one of three case-file sources:

| Source mode | Meaning | Required identity |
|---|---|---|
| `frozen_real_snapshot` | Queryable WAJEgame data from an accepted historical release | dataset, snapshot/release, coverage, query/result oracle |
| `controlled_synthetic_fixture` | Schema-compatible condition created to test incidents, latency, experiments or runtime races | fixture version/hash, generation/review authority, allowed claim scope |
| `known_contract_gap` | A required semantic or data capability is absent | backlog contract ref, affected claims, maximum ceiling |

One Episode may combine these source modes across different claims. Every claim
must state its support disposition independently. The user wording and valid
measurement-design space remain method-neutral.

This model preserves real WAJEgame capability, permits controlled testing of
rare or currently unobserved conditions, and prevents synthetic facts from
masquerading as current ClickHouse evidence.

## 5. Proposed repair order after the data-world decision

1. refactor the data-world and claim-disposition schema;
2. fix Agent observability and add the subset validator;
3. rebind all eight Episodes to resolvable facts and contracts;
4. rewrite and materialize all launch counterfactuals;
5. add the USER-008 prior-authority fixture;
6. define the authority observation bundle and milestone schedule;
7. rerun the three adversarial audits;
8. only then submit the Episodes for independent business-owner and measurement
   review.
