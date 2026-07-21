# WAJE BI v2 Implementation Roadmap

Last rebased: 2026-07-18
Architecture authority: [2026-07-17 single-authority workflow ADR](./adr/2026-07-17-single-authority-agent-workflow.md)

## Delivery objective

Ship one production-complete BI analysis workflow across all eight launch
question families. The workflow must preserve LLM exploration and writing
freedom while keeping business truth inside typed, content-addressed WAJE
records and hard evidence boundaries.

The cutover has no backward-compatibility path. Superseded mutable aggregate,
graph, recipe, replay-as-product, and duplicate compiler behavior is
deleted with the tests that require it.

## Authoritative path

```text
IntentRevision
→ DecisionLedger
→ AuthorityContext
→ PlannerProposal
→ ProposalAdmissionRecord
→ PlanRevision
→ CapabilityOutcome + EvidenceLedger
→ ClaimGraph + ClaimVerifierReport
→ sealed AuthorityBundle
→ NarrativeDocument + block-verifier report
→ PublicationProjection
→ delivery outbox
```

LangGraph schedules and exposes progress. It does not own the business truth in
these records.

## Working principles

- Bind open user language once through typed LLM output; local code validates
  structure and known contract IDs.
- Persist material baseline, time, scope, and comparison choices only in the
  `DecisionLedger`.
- Keep planner issue trees, hypotheses, and auxiliary axes before deterministic
  admission so unsupported ideas remain visible without becoming executable.
- Derive mandatory work from goals and claim obligations. A capability has no
  run-wide required flag.
- Let independent task failures affect only dependent obligations and claims.
- Keep observed, derived, statistical, candidate-mechanism, scenario, and
  boundary evidence distinct through claim settlement.
- Let the writer choose structure, emphasis, synthesis, and professional prose
  from the durably checkpointed `NarrativeMaterialProjection`. Verification may
  veto and cannot rewrite.
- Publish authority exactly once by bundle digest; deliver the fixed projection
  idempotently through the outbox.
- Use real Gateway, PostgreSQL, ClickHouse, and DeepSeek runs for business
  acceptance. Replays and fixtures remain diagnostic or contract-test inputs.

## Phase 0: Contracts and RED invariants

Deliver:

- immutable schemas, stable refs, digests, revision and supersession rules;
- typed failure taxonomy and orthogonal lifecycle states;
- hard checks that forbid downstream semantic reconstruction;
- real-chain acceptance entry and unique artifact directories.

Gate:

- current dependencies and active releases are reachable;
- Case B reaches the first legitimate material decision;
- no harness manufactures a plan, answer, or clarification response.

## Phase 1: Durable intent and decisions

Deliver:

- durable call journal and accepted transition head;
- `IntentRevision`, `DecisionLedger`, clarification writeback, correction,
  cancellation, and supersession;
- stable option IDs and typed free-text slot binding.

Gate:

- repeated model wording yields stable material bindings;
- one accepted baseline decision is reused after resume;
- process termination after intent or decision persistence resumes without
  replaying accepted semantic work.

## Phase 2: One planner/compiler and pinned data authority

Deliver:

- latest-active-release `AuthorityContext` per run attempt;
- immutable `PlannerProposal` and deterministic `ProposalAdmissionRecord`;
- one accepted `PlanRevision` with obligations, axes, task DAG, assumptions,
  budget, and contract versions;
- atomic stage persistence and replay from exact refs.

Gate:

- one planner proposal, admission record, and accepted plan digest exist;
- invalid auxiliary ideas remain recorded without a fallback proposal;
- all tasks use one release and snapshot set;
- Case B includes comparison, formula, eligible dimensions, temporal context,
  and data-quality obligations.

## Phase 3: Branch-isolated execution and evidence

Deliver:

- dependency-aware scheduler with stable idempotency keys;
- typed `CapabilityOutcome` for success, expected unavailability, integrity
  failure, technical failure, skip, and supersession;
- content-addressed `EvidenceLedger` with execution state, evidence kind,
  evidence ceiling, scope, window, dimension path, and limitation refs;
- formula graph compiled from reviewed metric/factor contracts;
- atomic `evidence_ready` checkpoint and replay.

Gate:

- completion order cannot change evidence identity or numerical results;
- an optional or branch-local failure does not erase independent evidence;
- shared integrity failures propagate through dependency edges;
- every terminal task closes to its admitted plan task and pinned authority.

## Phase 4: Claim settlement and authority seal

Deliver:

- evidence-bounded claims with stable logical keys and content revisions;
- explicit support edges, dependency claims, ceilings, assumptions, and
  limitations;
- obligation coverage and typed claim-verifier decisions;
- immutable `ClaimGraph` and content-addressed `AuthorityBundle` manifest;
- exact `user_required` obligation IDs sealed into the bundle manifest;
- atomic exactly-once authority seal.

`answer_verify` is a completion authority. It cannot appear as a capability task
or produce evidence.

Gate:

- every verified claim closes to intent, decisions, plan, authority context,
  evidence, and applicable contracts;
- unavailable obligations produce explicit boundary state;
- claim verification cannot create evidence or grant strength beyond a ceiling;
- narrative or delivery failure leaves the sealed bundle unchanged.

## Phase 5: Claim-aware narrative and publication verification

Deliver:

- lossless `NarrativeMaterialProjection` over claim-material pairs, pooled
  evidence facts, recommendations, limitations, boundary facets, and opaque
  publication requirements;
- provider fact bindings containing only claim and fact handles, with immutable
  values resolved locally from the projection;
- raw `NarrativeDocument` blocks with structured handles;
- local schema, handle, numeric, date, scope, and output-safety checks;
- one structural handle grammar shared by provider validation and typed block
  construction, including recommendation-authorized business blocks;
- semantic block verifier with veto-only authority;
- focused retry for rejected required blocks under centralized LLM policy;
- target-only retry output with deterministic mixed-origin revision merge;
- status-specific required-block coverage for every sealed `user_required`
  obligation.

Gate:

- accepted provider text is retained byte-for-byte;
- accepted typed blocks retain block ID, digest, and original writer-attempt
  provenance across focused repair;
- writer structure and emphasis are free within the supplied material projection;
- requirement coverage constrains handles only and leaves prose, block layout,
  ordering, emphasis, comparison, and synthesis with the writer;
- verifier-accepted required blocks close every publication requirement, or the
  narrative is withheld after focused repair;
- no local template supplies a high-value business answer;
- rejected blocks cannot leak into customer publication.

## Phase 6: Publication transaction and delivery

Deliver:

- deterministic `PublicationProjection` proving that it added no fact;
- atomic publication transaction binding bundle, narrative, verifier report,
  projection, and outbox record;
- idempotent delivery attempts with explicit retryable and permanent failure;
- fixed customer-safe Gateway projection;
- advisory six-dimension insight-quality review.

Gate:

- delivery retry never restarts analysis or narrative generation;
- a customer payload closes to one projection and one authority bundle digest;
- the final `PublicationFlow` gate independently revalidates every
  `user_required` obligation against published claim and limitation refs;
- identity changes history ownership and audit only;
- quality review cannot change or revoke sealed claims.

## Phase 7: Delete old authority and finish product acceptance

Remove:

- old aggregate-answer and mutable-plan runtime contracts;
- duplicate planners, compilers, semantic binders, and compatibility readers;
- fixed role/order narrative skeletons that do not protect a hard boundary;
- artifact continue/export routes that rely on obsolete mutable artifacts;
- eval assertions that require superseded capability or publication identities.

Acceptance sequence:

1. freeze the current contracts and pass the automated contract, unit,
   integration, fault-injection, Gateway, build, lint, and stale-reference
   suites;
2. run one fresh Case B through the real Gateway, PostgreSQL, ClickHouse, and
   configured LLM chain;
3. stop on clarification without an approved decision, typed failure,
   publication withholding, or delivery failure;
4. verify persisted authority closure, writer contract version, customer-safe
   projection, and delivery identity automatically;
5. generate the release manifest, update current documentation, capture one
   durable long-term memory note, and start the release runtime.

Completion requires the automated Case A-D and launch-question-family matrix,
weak-evidence and missing-contract boundaries, branch isolation, restart/resume,
duplicate dispatch, authority-seal idempotency, publication withholding,
delivery retry, single-analysis parity, and fixed safe projection to pass, plus
one post-freeze real-chain Case B run.

## Current status

The codebase is on the single-authority cutover path and the former authority
objects are being removed directly. Phase completion is determined by the
automated gates above and one persisted post-freeze real-chain acceptance. A
technical `completed` status alone does not close the roadmap.

The current closeout includes the required-publication contract prompted by the
failed `verified-03` Case B attempt: mandatory obligation closure must enter the
provider material projection and required-block repair path before publication.
That artifact remains failure evidence. The final automated verification suite,
one fresh Case B acceptance, release manifest, documentation, and runtime start
form the remaining closeout work. Optional insight or wording-pair reviews may
run after launch and cannot change sealed authority.
