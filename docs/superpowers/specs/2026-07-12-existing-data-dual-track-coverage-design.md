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

For an ordered composite question-family set, diagnostic applicability is
resolved per family before the capability union is formed. A diagnostic tag
may contribute obligations only to the families that list it as supported; it
cannot erase the base obligations of another valid family. A tag unsupported
by every persisted family is rejected with an explicit mutation. Clarification
is reserved for cases where two or more materially different contract-valid
routes remain after that deterministic partition.

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

Independence is proven from the authoritative capability binding and its
declared dataset, query, validation, and claim dependencies. A material gap
with empty `affected_capabilities` is global and conservative; it cannot be
used to infer an independent executable sibling. When a scoped gap is accepted,
the compiler partitions claim intents and ceilings by capability. The omitted
chain blocks only its affected claims and evidence. A ready sibling may persist
a verified claim only from its own complete binding, result, completeness, and
evidence provenance. Overlapping claim types do not transfer authority from an
omitted capability to a ready sibling.

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

The conversation store publishes verified result refs as reuse candidates only.
Candidate routing metadata cannot grant claim authority. After the current
AnalysisContract and QueryContracts are compiled, AnalysisRuntime resolves the
candidate's persisted query, rows, snapshot, completeness, and binding authority.
It validates the exact semantic query signature, current source releases,
permission scope, fixed-window membership, schema and row-content fingerprints,
and ready completeness state before materializing an authority-linked cache hit
for the current run. Only that successful validation produces a final
`ReuseDecision=\"reuse\"`; any mismatch produces an explicit rerun decision and
executes the current query.

A request that changes a material axis never reuses the earlier claim,
AnalysisContract, or answer. It may reuse an underlying query result when the
newly compiled QueryContract has the same exact semantic signature and the
persisted result already contains every required window and field. Fixed-window
sets are ordered canonically before query signing, so changing which already
covered baseline is primary does not create a physical-query cache miss; the
current run still creates its own AnalysisContract, evidence binding, verified
claim, and answer. Separate negative tests cover a changed required-window set,
signature, release, permission, schema, and completeness drift. Business
synonyms for full-sample scope are normalized before signature comparison; this
normalization applies to the whole intent class rather than an eval sentence.

The positive platform reuse evaluation uses the current-authority
`market_dashboard` path. Its two turns keep metric, full-sample scope,
permission, source release membership, and fixed-window membership identical,
while reversing only baseline priority. The unavailable-as-of
`paid_order_success` / `compare_periods` path remains a typed blocker and cannot
stand in for positive reuse. Eval acceptance reads one run-matched
`admin_audit` ReuseDecision, resolves the source result through the conversation
store's signed result-candidate authority, and requires that candidate to belong
to a completed earlier run in the same evaluated thread and topic. It then
validates both the source authoritative query chain and the current capability
binding chain. The source candidate payload, signature, result-ref record,
contract owner, query, rows, snapshots/releases, permission, completeness, and
binding must agree; the current binding must be ready and every current report
must be complete and analysis-ready. The signed candidate, ReuseDecision, and
current QueryExecutionRecord cache metadata must carry the same non-empty
candidate signature. The current run must resolve from the conversation store
under the evaluated run, thread, and topic, and every QueryContract in its
binding chain must be owned by the accepted current AnalysisContract. Source
and current AnalysisContracts have different run owners even when their
physical QueryContracts match. Acceptance still requires the exact source
result, current result, QueryContract, expected capability, and expected
dataset provenance. Nested, unpublished, same-run, cross-topic, non-prior, or
otherwise unbound reuse markers never satisfy acceptance.

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

The source run also persists a versioned, exact-shape, signed
material-authority envelope in its PostgreSQL `analysis_runs.request` payload.
The envelope separately records the source intent's primary and ordered
question-family set, primary and ordered target metrics, explicit components,
dimensions, context sources, claim intents, scope, and the canonicalized source
time-window value. Exact canonical baseline ids already present in source intent
are preserved separately. Narrative baseline structures are translated through
a shared closed set of stable business aliases and typed shapes, then
intersected with the reviewed canonical route. Terminal and nonterminal resume
use this same canonicalizer. Mixed canonical and typed candidates retain every
successfully mapped route baseline in canonical route order; reordering is
stable while removal changes the signed material. Bare ranges such as past 7
days, previous business day, near 7 days, or same period last week do not imply
an average, exact weekday, or relative-day contract without a typed shape.
Previous-day narrative mapping accepts exact aliases and a closed set of
metric-suffix labels; conjunctions, alternatives, and other composite labels
fail closed. Typed-shape parameters are binding constraints: unsupported or
conflicting window and lag values cannot be masked by a direct canonical id in
the same structure. When both a selected time-window baseline and
`baseline_candidates` are present, the candidate list is the authoritative
allowed set and every selected canonical id must be its subset. Unknown,
contradictory, partially overlapping, or unreviewed selections fail closed.
Route-added baseline dependencies remain solely in the route material
projection and cannot be misrepresented as original intent. The envelope also records the route's
diagnostic requirement tags that opened clarification and the locally validated
diagnostic
rejection history as exact typed route-control records. The signature binds all
of those fields to the source run, thread, and topic owner. The resume authority
resolver reads and validates that stored envelope and returns it beside the
AnalysisContract and clarification outcome. Mutable `original_intent`,
`material_slots`, prior-contract copies, and prior-route mutation history must
match or be ignored in favor of the envelope; they cannot replace it.

Terminal resume replays the signed time-window value and overwrites mutable
`baseline_candidates` with the signed reviewed route baseline ids before any new
planning. Candidate material is checked against the source envelope first, so a
changed canonical baseline or time window is rejected. A narrative spelling
that has the same reviewed canonical meaning is harmless and cannot alter the
bound intent. This keeps canonical route expansion available while preserving
the distinction between explicit source intent and compiler-added baselines.

The AnalysisContract remains the authority for compiled capabilities, query
dependencies, gaps, and claim ceilings. It is not used to reconstruct explicit
user intent: `scope.requested_metric_ids` may include compiler dependency
closure, and dataset requirements may combine metric-source and requested
context roles. Inferring original components or context sources from those
compiled fields would reject valid resumes and is prohibited. The signed
material envelope is contract metadata derived from the already persisted
source run; it introduces no new business data.

The resolver and compiler validate the reversible overlap between the two
authorities: ordered question families, ordered target metrics, and material
scope must agree exactly between the signed material envelope and the immutable
AnalysisContract. Two independently valid signatures cannot authorize
contradictory business axes. This cross-check does not extend to explicit
components, dimensions, or context roles whose compiled representation may
contain dependency closure or lose the original request role.

Ordinary `inherit_current` follow-up turns may carry prior-topic business
material only from signed result reuse candidates whose source run is
`completed`. After delivery verification and successful runtime-record
persistence, the source run stores both its signed material-authority envelope
and an exact copy of the authoritative signed AnalysisContract in the existing
PostgreSQL `analysis_runs.request` JSON. Failed, waiting, incomplete, or
owner-mismatched runs cannot provide follow-up context. The InMemory and
PostgreSQL stores expose the same completed-authority resolver; it joins the
existing run and AnalysisContract authority, verifies run/thread/topic owners,
the stored request copy, contract and material signatures, and the reversible
material/contract overlap, without a schema migration.

Completion finalization also writes exactly one immutable
`completed_material_authority_recorded` audit event in the same store operation.
Its canonical payload binds the complete material authority, contract ref,
contract signature, and contract/material digests to the run/thread/topic
owner. Finalization is idempotent only when the existing event, request copy,
material, and contract projection are exactly equal. A conflicting event or
more than one event fails closed. The completed resolver requires the unique
event and exact event/request/authority equality, so rewriting request material
and recomputing its standalone signature cannot replace finalized authority.
The first authority publication is allowed only while the run is
`running_workflow`; a historical `completed` run with no authority event cannot
be backfilled. A `completed` run is accepted only as an exact idempotent replay
of its unique existing event and request. The same atomic completion operation
also preserves the existing `run_status_changed` audit contract with an explicit
`completed` status; exact replay adds neither event again.

The InMemory finalizer stages deep-copied run and audit state and publishes both
with one swap only after every validation and audit append succeeds. An injected
audit failure therefore leaves status, request authority, and audit history
unchanged. The PostgreSQL finalizer locks the run, AnalysisContract, and existing
authority-event rows with separate `SELECT ... FOR UPDATE` statements and writes
the request copy, status, and two audit events in that same transaction. It does
not lock a nullable side of a `LEFT JOIN`. Both stores report the same typed
missing-run, missing-contract, missing-event, and duplicate-event failures. The
embedded contract copy must contain a signature and it must equal the
authoritative contract column exactly; the column cannot backfill a missing
embedded signature, and a separately valid material signature cannot mask
embedded contract drift.

`WorkflowRunResult` carries the completed material authority privately from the
workflow boundary to ConversationAgentCore. Any successful result that exposes
an AnalysisRuntime result or persisted runtime records must provide this carrier.
AgentCore validates and finalizes it only after delivery, Answer Package, asset,
and runtime-record persistence boundaries succeed. A missing or malformed
carrier fails the run and cannot establish a completed source authority.

Result reuse candidates are a follow-up index derived from that completed claim
authority. AgentCore publishes them only after completed-authority finalization
succeeds, so a finalizer failure leaves no failed-run result refs. If index
publication fails after finalization, the completed run and claim authority stay
completed. The store first recovers its write transaction, then records a typed
`followup_index_publication_failed` audit. Recovery or audit failure is caught as
a best-effort warning on the completed response with exact error type and detail;
it cannot roll the run back or turn verified claims into a failed analysis.
The shared recovery contract is a no-op for the InMemory store and an explicit
transaction rollback for PostgreSQL, so an aborted PostgreSQL write never blocks
the typed audit write that follows.

ConversationRuntime keeps claim/result reuse eligibility separate from
intent-material continuity. A same-topic result that requires a semantic rerun
must remain `rerun` and must never enter the public `reuse_candidates` channel,
but its completed material may enter the private prior-topic context when the
snapshot and contract still match and the current role can read its scope. A
snapshot, contract, or permission mismatch remains ineligible for both
channels. This lets a follow-up change dimensions, grouping, comparison order,
or another semantic axis without losing the authoritative metric, scope, time
window, and ordered prior-baseline context needed by the intent provider.

ConversationRuntime accepts prior-topic material only when every forwarded
material-context candidate first passes its existing candidate validation and
the store's indexed candidate-authority resolver, then matches that canonical
store record exactly before the completed-authority resolver runs. Candidate,
authoritative AnalysisContract, and execution material permission scopes must
agree, and the current role must be allowed to read that scope before provider
invocation.
Every completed-authority value is preflighted as an exact Mapping before any
field access; a scalar, sequence, or null value fails with the stable
`prior_topic_completed_authority_shape_invalid` integrity contract.
Multiple refs from one source run collapse to one authority only after every
ref validates; one bad ref fails the whole source-run context. Multiple
completed source runs may be
combined only when their canonical business-material projections are exactly
equal; a conflict fails with one typed, order-independent integrity error and
never chooses the latest run. The private prior-topic material context is
recorded in ContextManifest as a `context_only` material ref with
`can_support_claims=false`; it cannot itself authorize a claim or a result
reuse decision.

Immediately before the `business_intent` provider call, Workflow validates the
source authority and AnalysisContract again, then projects target metric,
scope, time window, and ordered prior baselines into
`bound_business_context`. The provider still returns a complete intent and the
normal intent validator remains authoritative over that response. Prior-topic
axes that the current question does not explicitly replace must be returned as
their exact canonical bound values; an explicitly changed axis must be returned
as a complete new canonical value. Null or empty required axes remain contract
failures and are never filled locally. Prior-topic material is never copied
onto the workflow request's public top-level business
axes, never overwrites provider output locally, and never falls back to topic
summary text or local intent inference. If a follow-up has no completed
authority, existing no-context behavior remains; malformed, drifted, or
conflicting authority fails typed before provider invocation.
When private prior-topic material exists, any non-empty top-level
`question_family`, `pattern_family`, `pattern_params`, `target_claim`, `target`,
`target_metric`, `scope`, `time_window`, `baseline`, or `baseline_candidates`
is a closed conflict and fails before the provider call. This gate prevents a
caller-owned request axis from shadowing the provider's complete intent output.

`execution_material` is an explicit WorkflowState channel. The compiler/runtime
node writes the already validated execution projection once, and later
query-gap persistence reads that same value when building the waiting Answer
Package. LangGraph must preserve this authority between nodes. A missing or
tampered value still fails the existing material-authority validator; no node
may reconstruct it from AnalysisContract fields, narrative time text, or a
topic summary.

The same reversible-axis check applies to the current terminal-resume proposal
before compilation. A clarification payload may repeat the signed family,
target, or scope, but it cannot replace any of them while carrying the prior
gap decision or clarification outcome. A changed family, target, or scope starts
a new analysis without the prior terminal authority.

The terminal AnalysisRuntime handoff reprojects every signed route-material
axis from the envelope after validating any repeated current-route or
clarification value. This includes ordered families and targets, components,
dimensions, reviewed route baselines, context-source roles, claim intents,
diagnostic tags, scope, and source time-window semantics. Target execution
material is never inferred from the narrative `time_window`: the source
AnalysisContract's exact `target_day` projection, canonical `as_of`, business
timezone, permission scope, and resolved fixed-window bounds are projected into
the signed material envelope. The envelope and immutable source contract must
match exactly on those reversible runtime axes. `context_sources` and
`requested_context_sources`, baseline candidate aliases, and target-window
aliases resolve through the same closed canonicalizers before comparison.
Unknown values and conflicts fail closed. Exact repeats are accepted, omitted
current values are restored from signed authority, and only non-material
clarification suggestions may remain sourced from the current LLM response.
This projection is repeated at the final runtime request boundary so later
route defaults, clarification-choice merging, caller `analysis_context`, or
caller role cannot override the validated source-topic material. Both
permission elevation and permission reduction require a separate typed
boundary and new contract. A terminal resume rejects any changed `as_of`,
target, previous-day, rolling-seven-day, same-weekday, pattern-history, or
anomaly-history bound before compilation.

The envelope therefore has a closed `execution_material` projection sourced
from the actual source AnalysisRuntimeRequest, current runtime registry, and
source compile outcome. Alongside the clock and permission fields above, it
binds filters, grain, explicit dataset requirements, metric and dimension
dataset overrides, requested-context-source aliases, source accepted graph,
runtime contract version and registry digest, run-mode authority class, and the
source QueryContract semantic signatures with snapshot membership. Every
signed source query also carries its exact `owner_capability_ids`, derived from
the source CapabilityExecutionPlans' required, optional, and validation query
refs. A source query without an authoritative owner, an unknown plan ref, or an
owner outside the source accepted graph fails closed. These are existing
compiler inputs or outputs; reuse candidates and attempted query signatures
remain run metadata and cannot authorize material. On resume, the accepted
graph may retain or remove only capabilities named by the signed degradation
choice; every unrelated source capability remains required and no new
capability may appear. The compiler and executor repeat an overlap check by
projecting the signed query set through that validated current ready graph.
Every source query with at least one retained owner is still required, including
a shared query when any owner remains; queries whose owners are all legally
removed may disappear. The current semantic-signature and snapshot set must
equal that projection, so 2-to-0 or 2-to-1 deletion without owner-authorized
graph removal, new queries, release drift, and snapshot drift all fail before
evidence publication even when both AnalysisContracts are independently valid.

All execution-material fields declared as exact string sequences reject
`Mapping`, mapping-proxy, `str`, and `bytes` values before iteration. This keeps
mapping keys from being reinterpreted as dataset requirements, requested
context sources, accepted capabilities, snapshot refs, or owner ids.

Signing canonicalization cannot change compiler meaning. In particular, grain
uses one strict compiler-and-authority canonicalizer: the omitted value resolves
to `window_id`, while explicit values must already be non-empty trimmed strings.
Leading or trailing whitespace fails at the source compiler entry, before
dimension binding and gap production, so a source unsupported-grain gap cannot
vanish because the authority projection later strips the same input.

Only local obligation rejection records may enter the signed route-control
history. Each record has the exact `action`, `capability`, and `reason` fields,
uses `action=rejected`, and carries a reviewed local rejection reason. Order is
stable and duplicates, unknown fields, provider-supplied reasons, and malformed
records fail closed. Resume restores this history into internal workflow state;
the mutable prior analysis route cannot authorize or extend it.

### 7. One provider-owned LLM attempt boundary

Production and live runs treat every high-value LLM output as typed runtime
material. Business intent, clarification questions and recommendations, route
explanations, evidence interpretation, claim text, terminal explanations, and
the final business summary must originate in the provider response. The shared
LLM client may normalize reviewed terminology, but it cannot replace a missing,
empty, wrong-typed, or still-unlocalized narrative value with a local template.
Those outputs fail with a typed LLM or LLM-contract error before publication.
Legacy fixture-only narrative helpers remain outside the production/live path
and cannot be selected by a real provider failure.

The reviewed clarification escape `tell the agent to do differently` is a
machine contract token even though it is displayed inside a user-facing options
array. Conversation orchestration, boundary decision, general clarification,
and query-gap clarification all import one neutral contract constant. Every LLM
surface receives the token in delimited input and an explicit system/task-level
exception to the Chinese narrative rule; structured conversation options use
the same exact label with a Chinese description. It must be copied
character-for-character as the final option. Translation, paraphrase, case
changes, whitespace changes, relocation, aliases, or local fallback fail with a
typed contract error. A provider boundary decision with `needs_question` has
exactly one question, two or three business options, the final escape, and a
recommendation copied from a business option. Other boundary statuses carry an
empty question list. Business options and recommendation explanations remain
natural Simplified Chinese. Local policy continues to own the choice action and
accepted-graph effect.

The provider client owns the only implicit retry loop and uses exactly
`DEFAULT_MAX_ATTEMPTS=3`. A LangGraph business node invokes its LLM task once;
the node wrapper records one checkpoint and does not invoke the node again after
a provider or output-contract failure. Clarification, terminal explanation, and
final summary code do not contain their own retry loops. A verifier-driven
answer repair is an explicit typed repair boundary and may make one new LLM
call followed by one new hard verification; failure after that pass remains a
typed terminal failure. It cannot be multiplied by a generic node retry.

Provider isolation uses one-way subprocess IPC. The parent waits for a result
and drains it before joining the child, so a valid large JSON payload cannot
deadlock on the pipe buffer. Success, provider error, abnormal exit, empty IPC,
and explicit timeout paths close both IPC endpoints, reap the child, and finish
the receiver with a bounded cleanup join. A configured positive timeout defines
one wall-clock deadline across receive and child join; only expiry of that
deadline may terminate a still-running provider child. Without that setting,
the parent continues waiting and naturally reaps the provider child.

For production/live intent binding, explicit request-bound values take
precedence and must themselves pass the closed contract. Otherwise the real LLM
must provide every material axis used by the compiler, including the canonical
question family, target metric, pattern family, scope, time window, and target
claim. Missing or illegal material cannot silently become
`pattern_explanation`, `paid_amount`, `intra_period`, `full_sample`, a default
window, or an empty baseline set. `baseline_candidates` is required as an
explicit JSON array in production/live runs. The business-intent LLM input must
carry `allowed_baseline_ids` and business-readable labels and semantics derived
from the shared canonical baseline registry. `baseline_candidates` may contain
only exact string ids copied from that closed list, in the user's requested
priority order. The target window is never repeated as a baseline candidate;
when the user requests no reviewed comparison baseline, the value is `[]`.
These vocabulary and shape rules belong in both the delimited input and the
business-intent prompt, so a provider can produce the strict contract without
guessing internal ids. The workflow still applies the shared semantic
canonicalizer as a compatibility and hardening boundary for reviewed aliases
and typed shapes, while rejecting strings, bytes, mapping containers, nested
sequences, duplicates, unknown forms, and conflicting typed shapes. This keeps
persisted reviewed material compatible without weakening the LLM's exact-id
output contract. The registry vocabulary guides the LLM but does not grant
execution authority; local compiler, permission, release, and evidence
boundaries still decide what can run and support claims.

The business-intent input also carries the reviewed public `allowed_scope_types`.
The existing-data public path currently exposes only `full_sample`; a full-sample
business request must therefore produce that exact machine id. Dimension and
segment requests belong in requested dimensions and filter contracts, not in a
free-text scope token. Production/live scope is canonicalized through the
reviewed generic aliases and then rejected when it is outside the closed public
scope list. The prompt also requires `ambiguous_slots` to contain only material
slots that remain unbound and would change the answer. A target metric, scope,
or time window already explicit in the current canonical output or supplied
bound context cannot simultaneously be declared ambiguous.

Optional advisory
`sub_intents` and `ambiguous_slots` may be absent or null, but a supplied value
must also be an actual sequence; container coercion cannot turn provider text or
mapping keys into planning input.
historical window, or a generic target claim. Stable alias canonicalization and
reviewed deterministic contract reconciliation remain allowed; sentence-based
inference and unreviewed material rewrites do not.

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

Persisted `AnalysisContract.question_families` is an ordered canonical set.
The first entry preserves the primary business route and later entries may add
orthogonal trust, evidence, or data-quality axes. Hard obligation review unions
the reviewed obligation resolution across every persisted family, preserves
registry ordering and stable de-duplication, and records the contributing
families for every capability. Authored scenario families remain expectation
inputs for mismatch reporting and cannot replace or narrow this persisted set.
Multiplicity alone is valid when every entry is a distinct canonical registry
family. Unknown, invalid, or duplicate entries fail closed.

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
