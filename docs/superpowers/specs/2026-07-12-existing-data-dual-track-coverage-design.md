# Existing-Data Dual-Track Coverage Design

**Date:** 2026-07-12

**Status:** Approved in conversation

## Goal

Use every currently available, reviewable data source to improve analytical
coverage, route stability, evidence preservation, and answer usefulness without
waiting for a new payment-attempt feed, a new internal-operation event feed, or
restoration of an old evaluation artifact.

The work has two acceptance tracks over one shared runtime foundation:

1. the fixed eight-question paid-amount evaluation; and
2. a platform-wide question-family by current-dataset coverage matrix.

## Current Evidence Inventory

The implementation may use only inputs already present or already registered:

- the existing paid-order archive at `/Users/luka/Downloads/dapan_pay_data.zip`;
- the existing ClickHouse paid-success fact tables;
- the active PostgreSQL releases for `market_dashboard` and
  `market_dashboard_channel`;
- the active PostgreSQL releases for `gameplay` and `gameplay_channel`;
- the active PostgreSQL release for `external_event`;
- existing query results, analysis assets, ContextManifests, ReuseDecisions,
  evidence records, and local evaluation artifacts.

Runtime availability remains authority-driven. Versioned contracts describe
what a capability requires and allows; they do not hard-code that a dataset is
currently available. PostgreSQL snapshot and release records decide whether a
specific run can execute a capability.

## Explicitly Excluded Inputs

This scope does not create synthetic substitutes for:

- `payment_attempt` source rows;
- maintained `internal_operation_event` rows;
- user-level joins that the existing source contracts cannot support;
- the missing historical fixed-evaluation artifact.

The contracts and answer boundary must continue to expose these gaps with an
owner, impact, and repair path.

## Architectural Decisions

### 1. One reviewed obligation contract

Extend `contracts/runtime/clickhouse-analysis-bindings.yaml` with reviewed
question-family and diagnostic-obligation sections. The sections declare:

- required capabilities;
- conditional capabilities activated by requested dimensions, baselines,
  components, anomaly review, event context, or trust review;
- capabilities that may execute independently of the primary paid source;
- minimum publishable evidence;
- allowed degradation when an input is unavailable; and
- the owner of a missing capability contract.

The contract covers stable business intent classes. It cannot contain one of
the eight evaluation sentences, an evaluation case id, or a rule tied to a
single observed LLM response.

`RuntimeContractRegistry` validates complete coverage for every public question
family and every referenced public capability. Duplicate, unknown, empty, or
contradictory obligations fail contract loading.

### 2. LLM proposes; compiler reconciles

The real LLM continues to classify business intent and propose an analysis
route. A local reconciler evaluates the proposal against the reviewed
obligation contract using only typed intent fields:

- primary and secondary question families;
- target metrics and requested components;
- requested dimensions;
- requested baselines and fixed windows;
- context-source requirements;
- diagnostic requirement tags; and
- intended claim types.

The reconciler adds uniquely required capabilities, removes illegal
capabilities, and records every mutation. If more than one materially different
valid route remains, clarification can be opened. Low-risk omissions use the
reviewed recommended route and persist the assumption.

The existing hard-coded family enablement and revenue diagnostic bundles move
behind this contract boundary. Stable business-language intent extraction may
remain, but evaluation sentences and case ids cannot become routing inputs.

### 3. Available capability paths execute independently

One unavailable dataset must affect only the capabilities that depend on it.
The compiler creates query contracts for every independently executable path:

- paid-success analysis from a registered paid snapshot;
- market overall and channel analysis;
- gameplay overall and channel context;
- external-event interval context; and
- data-quality and source-reconciliation paths.

A missing paid or payment-attempt input cannot erase a valid market, gameplay,
or external-event query. Capability plans and contract gaps retain exact
ownership, result refs, completeness refs, and accepted evidence ceilings.

### 4. Register the existing paid-success facts as authority

Add a narrow registration workflow for the current paid-order archive and
ClickHouse tables. It must:

- validate the source archive checksum and reviewed source contract;
- inspect the existing physical table schema, row count, date range, and
  business-date watermark;
- validate successful-payment semantics and the currently supportable unique
  key boundary;
- calculate immutable schema and row-content fingerprints;
- create a PostgreSQL DatasetSnapshot and atomic release; and
- refuse publication on any mismatch.

This workflow registers existing facts. It does not invent payment-attempt
rows, infer missing statuses, or claim payment-success-rate coverage.

### 5. Preserve partial verified value

When some capability chains verify and others remain unbound, the Answer
Package preserves the verified claims and presents the gaps separately. The
business answer must say:

- what current data confirms;
- what remains unverified;
- which missing input limits which conclusion;
- which factors were not tested; and
- the next useful business action.

Generic source-unavailable prose cannot replace available market, gameplay, or
event evidence. Permission, SQL safety, contract legality, query completeness,
claim provenance, and the evidence verifier remain hard boundaries. Final LLM
quality audit remains warning-only unless a hard boundary is violated.

### 6. Reuse before rerun

Exact-match snapshots, query results, completeness records, and analysis assets
may be reused only through a validated ReuseDecision. Reuse requires matching
contract signatures, fixed windows, permission scope, schema fingerprint,
source releases, and completeness state.

Known source gaps should not trigger repeated clarification. Once the user
accepts the reviewed degraded route, the run resumes the original topic and
executes every remaining valid capability.

Clarification resume is authority-bound. The selected degradation choice is
persisted as an immutable clarification-outcome record owned by the source run,
thread, and topic. Resume resolves that record together with the source
AnalysisContract from PostgreSQL authority and verifies the exact stored
payload, contract signature, run owner, and selected choice before compilation.
Mutable analysis-run request JSON is context only and cannot authorize carried
gaps. A carried gap is narrowed to the exact intersection of the persisted
accepted choice and the prior canonical gap, then adds `analysis_contract` as
the control boundary. The accepted choice remains a contract requirement even
when its degraded capability is intentionally omitted from the resumed graph;
capabilities outside that choice are removed. Malformed
capability mappings, stale records, owner drift, signature drift, and choices
without a resolvable outcome ref fail closed.

## Coverage Audit

Add a local coverage-audit tool that combines the reviewed runtime registry
with PostgreSQL authority. It emits, per question family and capability:

- required and conditional obligation status;
- required datasets and current release refs;
- executable, degraded, source-unbound, contract-partial, or
  permission-blocked state;
- supported metrics, dimensions, windows, evidence types, and claim ceilings;
- exact owner and next action; and
- a machine-readable coverage summary.

The artifact remains local under `artifacts/`. No PostgreSQL product page or UI
is added.

## Acceptance Track A: Fixed Eight Questions

Run the existing fixed-clock case twice through `ConversationAgentCore` with
real LLM, ClickHouse, and PostgreSQL.

Acceptance requires:

- all eight questions reach a user-visible terminal answer;
- clarification resumes the original topic;
- every capability required by the reviewed obligation contract is accepted,
  explicitly degraded, or blocked with an exact contract gap;
- hard acceptance evaluates persisted capability outcomes, not accepted-graph
  membership: each required capability must be `executed`, `degraded`, or
  `blocked`; `blocked` requires a typed persisted contract gap whose
  `affected_capabilities` names that exact capability, while `unobserved` and
  `missing_route` always fail;
- the accepted AnalysisContract persists compiler-owned requested metric and
  dimension identities, and blocked-gap review uses the compiler's complete
  gap-ID grammar plus those identities; a namespace, marker prefix, or client
  package alone cannot establish a terminal gap;
- `executed` and `degraded` may be established by a persisted capability
  binding or by the run-matched admin audit's validated capability execution
  plan. The plan route must preserve the reviewed capability ref/signature and
  an exact plan-slot -> query-contract -> result -> completeness chain; summary
  booleans and client packages do not establish an outcome;
- when coverage authority resolves expected states per capability, hard
  acceptance compares those exact capability states with terminal outcomes.
  Dataset-level state collapse remains reporting-only in that mode, so an
  unrelated partial capability on the same dataset cannot overwrite an
  executed capability; dataset aggregation remains the legacy fallback when
  no capability-state authority exists;
- terminal-boundary resolution follows the same authority mode: capability
  state authority may refine the reviewed boundary, while dataset collapse is
  reporting-only and cannot rewrite `verified_answer` because of an unrelated
  partial sibling capability;
- every currently executable required query has snapshot, result,
  completeness, binding, ContextManifest, and ReuseDecision authority;
- market, gameplay, and external-event evidence survives unrelated paid-source
  gaps;
- payment-attempt and internal-operation claims remain blocked;
- no executed query is silently truncated or marked complete after failed
  assertions; and
- both artifacts receive a run-matched real final-LLM audit and a nonblocking
  quality scorecard.

The track is allowed to fail overall strict source coverage while excluded
inputs remain absent. It must pass every current-data obligation and must not
misclassify an excluded input as available.

## Acceptance Track B: Platform Coverage Matrix

Create reviewed cases spanning every public question family and every currently
available dataset role. The matrix includes:

- paid-success metrics and formula components;
- market overall and channel metrics;
- gameplay overall and channel metrics;
- external-event context;
- independent and joint dimension routes where current contracts permit them;
- target, previous-day, rolling, same-weekday, pattern-history, and
  anomaly-history windows;
- data-quality, source-reconciliation, and evidence-boundary questions; and
- reuse and clarification-resume cases.

Each matrix cell has an expected capability set, dataset state, evidence state,
claim ceiling, and terminal status. Unsupported cells must degrade with a typed
reason; they cannot disappear from the report.

## Testing Strategy

Every implementation task follows red-green-refactor and receives an
independent review before commit.

Test layers:

1. contract and registry schema tests;
2. compiler obligation and mutation tests;
3. dataset snapshot and release authority tests;
4. query compiler and completeness tests for every current source adapter;
5. capability execution and partial-evidence tests;
6. Answer Package, ContextManifest, ReuseDecision, and verifier tests;
7. ConversationAgentCore clarification and fixed-clock tests;
8. deterministic coverage-matrix tests; and
9. two real fixed evaluations plus a platform slice with real services.

No test may require a sentence-specific rule or a fabricated data source.

## Delivery Sequence

1. versioned obligation and coverage contracts;
2. existing paid-success registration authority;
3. contract-driven route reconciliation;
4. independently executable current-data paths;
5. current metric, dimension, and window query closure;
6. partial verified answer preservation and reuse;
7. platform coverage audit and matrix;
8. fixed and platform real evaluations;
9. final independent review and delivery audit.

## Success Definition

The release is successful when every capability that current authoritative data
can support is discoverable, executable, completeness-checked, and usable in a
verified business answer; every capability that requires excluded data remains
visible with an exact boundary; and both acceptance tracks can be rerun from
documented commands without hidden fixtures or sentence-specific behavior.
