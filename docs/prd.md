# WAJE BI v2 Product Requirements

Status: current
Architecture baseline: 2026-07-17 single-authority workflow
Last rebased: 2026-07-24

## 0. Product authority

WAJE BI v2 is a clean-slate BI Agent. The first production baseline covers all
eight launch question families with real data, real model calls, auditable
evidence, explicit claim boundaries, verified business writing, and durable
delivery.

The current workflow authority is:

```text
IntentRevision
→ DecisionLedger
→ AuthorityContext
→ PlannerProposal + ProposalAdmissionRecord
→ PlanRevision
→ CapabilityOutcome + EvidenceLedger
→ ClaimGraph + ClaimVerifierReport
→ sealed AuthorityBundle
→ NarrativeDocument
→ PublicationProjection
→ delivery outbox

customer delivery
→ NarrativeQualityAuditResult (independent advisory record)
```

Each kind of business truth has one writer. Downstream stages reference an
upstream record, add a new revision, enforce its declared hard boundary, or
create a fixed safe projection. They cannot reconstruct or silently change
upstream facts.

## 1. Product goal

Users ask natural-language business questions and receive an investigation that:

- binds the intended metric, scope, time semantics, baseline, and desired
  decision;
- uses the latest active data release and keeps it pinned for the run attempt;
- explores the relevant formula, dimension, temporal, anomaly, event, and
  quality paths within reviewed contracts;
- separates observed facts, accounting contributions, associations, candidate
  mechanisms, scenarios, and evidence boundaries;
- publishes useful business synthesis with traceable claims and limitations;
- exposes business-readable progress without exposing hidden reasoning, raw
  rows, secrets, or unsafe identifiers;
- can resume after process interruption without repeating accepted semantic
  work or changing authority.

The product optimizes for decision value under evidence constraints. A polished
but unsupported conclusion fails. A mechanically correct data recap with no
business synthesis also fails quality review.

## 2. Core principles

### 2.1 SQL first, evidence first

Quantified claims require validated query/result authority, completeness,
snapshot and release provenance, contract versions, and a supported evidence
ceiling. SQL generation, parameter binding, grain, fixed output safety, and data
access remain deterministic hard boundaries.

### 2.2 Open semantics stay with typed LLM bindings

LLMs own open-language intent binding, ambiguity discovery, business issue
trees, auxiliary axes, candidate hypotheses, evidence interpretation,
professional writing, and semantic entailment review.

Local code owns known IDs, dates and windows, permissions, SQL safety, release
selection, contracts, formulas, statistics, completeness, reconciliation,
digests, provenance, persistence, and fixed customer projection. It does not use
keyword dictionaries to guess corrections, causal meaning, or terminal business
meaning from free text.

### 2.3 Wide exploration, strict settlement, free expression, narrow publication

The planner retains business-readable hypotheses and auxiliary ideas before
admission. Deterministic admission controls execution. Claim settlement enforces
evidence classes and ceilings. The writer controls structure and emphasis over a
durably checkpointed `NarrativeMaterialProjection`. Local narrative validation
enforces schema, handles, numbers, dates, scope, fixed output safety and
provenance. Subjective completeness, depth, readability and actionability are
recorded after delivery for Workbench and human review; they cannot change,
delay, withdraw or rewrite the publication.

Mandatory answer completeness is expressed as opaque publication requirements,
not a paragraph template. These requirements constrain claim and limitation
handles carried by required blocks while leaving wording, ordering, emphasis,
comparison, and synthesis with the writer.

### 2.4 Branch-local failure

Task and evidence dependencies define failure radius. An unavailable optional
factor affects its dependent claims. It cannot erase successful independent
analysis or become a zero-impact conclusion. Shared release corruption, unsafe
SQL, invalid ownership, or broken digest closure may block all dependent paths.

### 2.5 One analysis capability for all normal users

User identity controls conversation history ownership, audit, rate limits, and
performance safety. It does not change datasets, snapshots, plan admission,
evidence strength, claims, or publication strength.

Customer output uses one fixed safe projection. Raw identifiers, raw rows,
secrets, internal owner/debug fields, and unrestricted provider payloads remain
server-side.

## 3. Users and tasks

The primary user is a business operator, analyst, or decision owner asking:

- what changed and by how much;
- which accounting components or business segments explain the movement;
- whether a recurring pattern or anomaly exists;
- whether an activity, campaign, incident, or external event may matter;
- how healthy a revenue metric is;
- whether the evidence is strong enough to act on;
- what to investigate or do next.

The same question may bind multiple families. The active `IntentRevision`
records the goals; the accepted `PlanRevision` records one executable analysis
route with explicit claim obligations.

## 4. Launch question families

| Question family | Required business outcome | Typical executable capabilities | Main claim boundary |
|---|---|---|---|
| `paid_amount_change_explanation` | direction, magnitude, formula reconciliation, material dimensions, limits | `data_quality_check`, `formula_decompose`, `segment_bridge`; conditional pattern, anomaly, event, attribution | accounting contribution is distinct from mechanism or cause |
| `pattern_explanation` | pattern existence, effect size, stability, exceptions, candidate context | `data_quality_check`, `pattern_scan`; conditional event, segment, anomaly | recurrence supports a pattern; context does not prove cause |
| `business_object_impact_review` | object window, exposure/control availability, observed movement, supported impact wording | `data_quality_check`, `event_evidence`; conditional formula, segment, attribution | net impact or causal wording requires qualified controls |
| `revenue_health_review` | trend, baseline, formula health, structure, anomaly and data risk | `data_quality_check`, `formula_decompose`, `outlier_scan`; conditional segment and attribution | health judgment names affected metric, scope, and data risks |
| `segment_or_factor_attribution` | localized movement, contribution where additive, interaction limits | `data_quality_check`, `segment_bridge`; conditional `joint_attribution`, formula | overlapping dimensions cannot be summed as independent contribution |
| `anomaly_or_black_swan_review` | anomaly validity, affected scope, ruled-out paths, candidate context | `data_quality_check`, `outlier_scan`, `event_evidence`; conditional segment and attribution | unusual timing or external context remains candidate evidence without causal support |
| `custom_baseline_comparison` | explicit target/baseline semantics, comparability, delta, contextual disagreement | `data_quality_check`, `pattern_scan`, `formula_decompose`; conditional event and attribution | primary baseline controls the main comparison; context cannot replace it silently |
| `data_quality_or_evidence_review` | claim trust, contract coverage, gaps, affected claims, upgrade path | `data_quality_check` plus referenced evidence review | a trust judgment is scoped to named claims and boundaries |

Every family requires the `answer_verify` completion authority after evidence
execution. It is not a capability task and produces no analytical evidence.

## 5. Intent, decisions, and clarification

### 5.1 Intent revision

`IntentRevision` records original user text, its customer-facing
`business_summary`, goal and metric bindings, scope, time specification,
direction premise, requested axes, desired decisions, ambiguity slots, source
spans, prompt/model versions, and content digest. The LLM returns the summary
and typed binding together. The UI displays it only after the revision is
accepted and bound to the run.

Observed direction cannot enter intent. It becomes a claim only after target and
baseline evidence is complete.

### 5.2 Decision ledger

Material baseline, time, scope, comparison, and similar choices live in the
`DecisionLedger`. Display text has no decision authority. Stable option IDs write
back to a named slot. Free text is bound by the LLM to that slot.

A material goal, metric, time, or scope change creates a superseding run attempt
and intent revision. A simple slot resolution keeps the active intent revision.

### 5.3 Question tool

Clarification is optional and blocks only when unresolved ambiguity can change a
business conclusion, baseline, time semantics, fixed safe-output boundary,
evidence ceiling, or material execution cost.

The LLM provides two or three business options, a recommended option with a
short explanation, and a free-text path to instruct the agent differently. Low
risk gaps use an explicit safe inference recorded in the ledger and continue.

User role and data-capability level never appear as clarification options.

## 6. Planning and data authority

### 6.1 Authority context

Before the first plan, the runtime resolves the latest active release set and
persists `AuthorityContext`: actual `as_of`, release refs, snapshot refs, dataset
coverage, contract versions, and digest. Every task and plan patch in the attempt
uses the same context.

### 6.2 Planner proposal and admission

The planner returns an immutable business proposal containing issue-tree nodes,
analysis axes, hypotheses, priorities, assumptions, and provider audit refs.

Deterministic admission records each proposal item as admitted, rejected, or
deferred with a closed reason code and contract refs. Structurally invalid model
output remains a failed model attempt. The runtime does not fabricate an empty
proposal or invoke a second compiler.

### 6.3 Plan revision

`PlanRevision` contains intent and decision refs, authority context, planner and
admission refs, resolved windows, claim obligations, admitted axes, capability
tasks, assumptions, budget policy, contracts, and digest.

Mandatory tasks derive from goals and obligations. The LLM may add supported
auxiliary routes. Ranking controls execution priority and display priority; it
does not decide whether a qualified observation exists.

## 7. Execution and evidence

Each `CapabilityTask` has normalized input refs, dependencies, obligation edges,
execution policy, and an idempotency key derived from plan, input, release, and
contract identity.

Each terminal attempt returns a typed `CapabilityOutcome`:

- `succeeded`;
- `unavailable`;
- `integrity_failed`;
- `technical_failed`;
- `skipped`;
- `superseded`.

Expected data gaps use typed outcomes. Exceptions remain process or integrity
faults that cannot be represented safely.

The `EvidenceLedger` preserves all qualified evidence with orthogonal fields for
execution state, evidence kind, data-contract state, supported claim kinds,
maximum strength, observation facts, scope, windows, dimension path, and
limitations. A display preference can rank evidence and cannot replace the
support set.

## 8. Evidence and claim classes

The runtime keeps these epistemic classes distinct:

- observed fact;
- accounting identity contribution;
- dimension localization;
- statistical association;
- candidate mechanism;
- causal effect;
- scenario;
- boundary.

Missing state has no neutral verified default. A hypothetical payment-success
rate is a scenario and cannot become an observed value.

Claims have a stable logical key and a content revision. Support edges may
support, qualify, depend on, contradict, or contextualize. Every claim records
its evidence ceiling, assumptions, limitations, scope, baseline, and provenance.

Candidate mechanisms may remain visible and useful at candidate strength.
Causal effect requires a reviewed causal design and supporting evidence.

## 9. Claim settlement and completion authority

Claim settlement creates proposed claims from qualified evidence, then invokes
the `answer_verify` completion authority. The verifier produces a
`ClaimVerifierReport` with accepted/withheld mappings, veto reasons, and complete
obligation coverage.

The verifier may:

- veto a claim that exceeds evidence strength;
- require an explicit boundary for unavailable or missing-contract obligations;
- reject broken scope, baseline, decision, evidence, contract, or digest closure;
- constrain candidate and causal language through the publication ceiling.

The verifier cannot create evidence, grant a stronger claim, repair data, or
write the customer answer.

The resulting `ClaimGraph` and immutable `AuthorityBundle` are sealed exactly
once by digest. The bundle references child records and does not copy their full
payloads into a mutable aggregate.

The bundle also seals the exact IDs of accepted plan obligations whose role is
`user_required`. Auxiliary analytical obligations remain available for insight
and audit but do not become mandatory customer-answer items.

## 10. Narrative and customer publication

The writer receives claim, material, fact, recommendation, limitation, and
boundary-facet handles from the durable public-safe
`NarrativeMaterialProjection`. It does not receive the derivation palette, raw
rows, SQL, secrets, owner/debug fields, or unrestricted evidence payloads.
Its material bindings return only claim and fact handles. The runtime resolves
the complete immutable fact tuple from the projection before local validation.

`NarrativeDocument` preserves the model's business prose and structured handles.
The writer controls ordering, emphasis, synthesis, and professional style. The
runtime does not require a fixed ordinary paragraph skeleton.

Provider validation and typed block construction share one structural handle
grammar. A non-boundary block is authorized by at least one claim or verified
recommendation; a boundary block carries at least one limitation; `next_action`
carries a verified recommendation. Any block that binds a recommendation may
also bind that recommendation's verified risk limitations. Claim-to-limitation
scope remains a local block-validation decision so a correctable mismatch can
enter focused repair.

For every sealed `user_required` obligation, the material projection supplies an
opaque publication requirement with status, required claim strength, eligible
claim handles, and required limitation handles. It contains no user-facing
obligation ID or internal basis/coverage ref. Required narrative blocks must
collectively satisfy the following closure:

- `satisfied`: publish at least one listed claim that reaches the required
  strength and no coverage limitation;
- `mixed`: publish at least one accepted coverage claim and every coverage
  limitation;
- `contradicted`: publish at least one accepted coverage claim and every listed
  limitation;
- `unavailable`: publish no claim for the requirement and publish every coverage
  limitation.

Local validation checks schema, handle membership, material facts, numbers,
dates, scope, and fixed output safety. The semantic block verifier checks
entailment and claim strength. Accepted blocks retain their typed identity,
digest, provider text, and original writer-attempt provenance. Rejected required
blocks may receive one focused writer attempt under the centralized LLM policy.
That provider response contains only replacement targets. The runtime merges
those targets with accepted source blocks in source order and records a
mixed-origin narrative revision. Rejected auxiliary blocks may be omitted with
an audit record.

Publication readiness uses verifier-accepted required blocks only. If a verifier
veto removes mandatory coverage, focused repair must restore the missing handles
or the answer is withheld. `PublicationFlow` then repeats the closure check over
the resolved customer payload as the final hard gate.

`PublicationProjection` contains accepted and omitted block IDs, display order,
field policy, visualization refs, warnings, and digest. It may remove fixed
fields and choose deterministic display order. It cannot add a claim or increase
strength.

The publication transaction binds one sealed bundle, narrative revision,
verifier report, projection, and delivery-outbox record. Delivery retry does not
restart analysis, reseal authority, or rewrite narrative.

## 11. Answer experience

The default customer experience includes, when supported:

- a direct executive conclusion;
- primary direction and magnitude under the chosen baseline;
- material accounting drivers and dimension localization;
- relevant patterns, exceptions, anomalies, or business context;
- evidence boundaries and unavailable paths;
- a decision-oriented next action with conditions and risks;
- visualizations whose semantics and numbers close to verified claims.

Card count, block count, and ordering are flexible. A run may publish a verified
boundary-only conclusion when all admissible data paths are unavailable. It may
withhold publication when required blocks remain unsupported.

## 12. Factor SSOT and capability contracts

`contracts/ssot/付费金额影响因子分析.mm` is the business SSOT for metric and
factor relationships, formulas, dimensions, known gaps, and analysis routes.
Reviewed build-time compilation produces versioned runtime contracts and a
formula graph.

The factor master records what exists. Capability support records state what the
current system can execute and the maximum evidence it can produce. Missing
contracts have explicit backlog refs and affected question families.

Executable capability cards cover:

- `pattern_scan`;
- `formula_decompose`;
- `joint_attribution`;
- `event_evidence`;
- `outlier_scan`;
- `outlier_contribution`;
- `segment_bridge`;
- `segment_contribution`;
- `data_quality_check`.

Each card defines business uses, non-uses, required/optional parameters, typed
evidence output, degradation, lint rules, verifier hooks, and supported families.

The separate `contracts/authorities/answer-verify.yaml` contract defines the
completion authority. It never appears in `PlanRevision.capability_tasks`.

## 13. Formula, dimensions, patterns, and attribution

### 13.1 Formula graph

The generic formula engine supports reviewed multiplication, addition, ratio,
bridge, and hierarchy expressions. Quantified decomposition requires component
coverage and reconciliation. Missing components remain missing; scenarios stay
separate from observations.

### 13.2 Dimension localization

Every contract-compatible axis enters the candidate universe. Execution order
uses expected information gain, unexplained movement, materiality,
actionability, statistical risk, and cost.

Successful qualified region, city, device, channel, payment, user-value, or
other paths retain evidence. Material or explicitly requested findings form
child claims. Overlapping dimensions require a qualified joint-attribution
method before additive percentages can be published.

For WajeGame, country is normally invariant and non-diagnostic because the
current product scope is Nigeria. Routine localization starts with region and
city. Country becomes eligible for cross-country, data-quality, or explicit
product-scope questions through the business-context contract.

### 13.3 Baseline and temporal context

The confirmed primary baseline controls the main direction and amount. Extra
windows provide context and stability. Baseline disagreement is publishable and
must name the primary result plus the contextual difference.

### 13.4 Pattern and exploratory evidence

Pattern evidence names window definitions, recurrence, magnitude, stability,
exceptions, materiality, and data quality. Cumulative month-to-date values cannot
support within-month daily-shape conclusions.

Large dimension, lag, or window searches use reviewed multiplicity and stability
policies. Time-series association checks trend, autocorrelation, lag search, and
leakage. Association may support a candidate mechanism within its ceiling and
cannot establish cause.

## 14. First vertical slice

The first end-to-end slice remains the historical failure question:

> 全量样本看，为什么从 2024 年 1 月开始到 2026 年 5 月结束，每个月月初的付费金额都比月中/月末高一些？

Expected binding:

- family: `pattern_explanation` with quality and factor review;
- metric: `paid_amount`;
- scope: full sample;
- time: 2024-01 through 2026-05;
- phases: day 1-10, 11-20, 21-month end;
- direction premise: user hypothesis only;
- primary comparison: same-month phase;
- grain: daily observations aggregated into month-phase evidence.

Required obligations:

- metric and source quality, time-zone and cumulative-value boundary;
- pattern existence, magnitude, stability, exceptions, and tested windows;
- eligible event/payday context at candidate strength;
- material formula, segment, or anomaly paths when admissible;
- explicit missing-contract and unavailable-path boundaries;
- claim settlement and customer publication verification.

The run fails acceptance if it reclassifies the question as period-over-period,
uses cumulative values for daily shape, invents source rows, treats payday as a
confirmed cause, hides exceptions, omits material boundaries, or publishes a
claim without complete authority refs.

## 15. Runtime and service boundary

The TypeScript frontend and Gateway own presentation, authenticated conversation
entry, customer-safe projections, and HTTP delivery.

The Python BI Agent Core owns intent/decision bindings, planning, admission,
query and capability execution, evidence, claim settlement, authority sealing,
narrative verification, publication transactions, and durable transition logic.

PostgreSQL stores conversation and authority records, accepted transition heads,
provider audit refs, publication records, and outbox state. ClickHouse provides
released analytical data. DeepSeek handles typed semantic tasks through the
central LLM client.

Timeout, retry, model routing, and provider circuit policy live in the provider
layer. High-value semantic nodes wait for a real response by default. Business
nodes do not contain local retry loops or template answers.

## 16. Durability and lifecycle

The runtime records stable event and parent IDs, input/output digests, intent and
plan revisions, release and contracts, attempt number, retry reason, state, and
next transition before advancing across an authority boundary.

Execution and LLM calls use at-least-once semantics. One attempt is accepted for
each durable transition. Authority sealing is exactly once by digest. Delivery
is idempotent and retryable through the outbox.

Interaction, analysis, publication, delivery, retry, cancellation, and
supersession states remain orthogonal. A delivery failure cannot change analysis
or claim state.

## 17. Launch evaluation

Launch evals combine natural user wording with structured expectation packages.
The sample pools are:

- real user questions;
- historical failure cases;
- matrix-generated boundary cases.

Expectation packages define intent boundaries, required/optional/forbidden
capabilities, required completion authorities, expected compiler actions,
contract/evidence states, allowed claims, semantic evidence records,
visualization expectations, required-publication closure, verifier checks, and
failure attribution.

Gold standards follow this maintenance path:

```text
business-reviewed gold standard
→ system-generated draft cases
→ human review
→ accepted eval package
```

An eval failure does not automatically become a runtime guardrail. Promotion
requires a generalizable failure pattern, human validation, and joint business
and system ownership. Hard legality, safety, contract, and provenance rules
remain code or contract boundaries.

## 18. Acceptance matrix

Each launch cell crosses question family with factor/capability support and has
independent `business_evidence_state` and `data_contract_state`.

Main conclusions must degrade or be omitted when the relevant path is
`missing_contract`, `unsupported_grain`, source-unbound, permission-blocked, or
below the claim-type evidence threshold. Supported independent claims remain
publishable at their qualified strength.

Production acceptance requires:

- all eight question families and original/paraphrase authority stability in
  the automated expectation and scenario matrix;
- automated Case A-D coverage plus one fresh post-freeze Case B success through
  the real Gateway, PostgreSQL, ClickHouse, and configured LLM chain;
- current release consistency and exact contract/query/result provenance;
- branch isolation, shared failure propagation, restart/resume, duplicate
  dispatch, seal idempotency, publication withholding, and delivery retry;
- status-specific customer publication for every sealed `user_required`
  obligation, including focused repair after a required-block veto;
- target-only focused-writer responses, deterministic mixed-origin merge, and
  unchanged typed provenance for accepted sibling blocks;
- fixed customer-safe projection and single-analysis parity across users;
- no local high-value answer fallback;
- no obsolete workflow authority, duplicate semantic binding, or compatibility
  reader in the active path.

Manual truth review, manual insight scoring, and wording-pair review are
optional post-launch evaluation inputs. They do not gate publication,
deployment, or an individual customer answer.

## 19. Observability

Business-visible events show intent, material decisions, data authority, admitted
plan, capability progress, branch limitations, claim settlement, narrative
verification, publication, and delivery in business language.

Admin audit retains stable causal refs, versions, digests, timing, cost, retry
reason, failure scope, and controlled provider/query details. Hidden reasoning is
never exposed.

Required operational indicators include publishable-answer rate, repeated
clarification rate, auxiliary-to-global failure amplification, verified-claim
retention, verifier veto reasons, time to first material decision, time to final
answer, cost per accepted analysis, release freshness, resume success, and
duplicate-publication rate.
