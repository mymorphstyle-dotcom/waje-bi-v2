# WAJE BI v2 Implementation Roadmap

Last rebased: 2026-07-24
Architecture authority:
[2026-07-17 single-authority workflow ADR](./adr/2026-07-17-single-authority-agent-workflow.md)
and
[2026-07-20 advisory publication ADR](./adr/2026-07-20-advisory-publication-human-review.md)

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
→ NarrativeDocument
→ PublicationProjection
→ delivery outbox

customer delivery
→ NarrativeQualityAuditResult (independent advisory record)
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
  from the durably checkpointed `NarrativeMaterialProjection`. Local structural,
  numeric, scope, reference and output-safety validation remains hard; subjective
  answer-quality findings stay in a post-delivery advisory record.
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

## Phase 5: Claim-aware narrative and post-delivery quality audit

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
- one publication-ready narrative revision produced from the sealed material
  projection;
- an independent post-delivery quality-audit result, created only after the
  audit finishes as `completed` or `unavailable` and linked back to the already
  delivered customer publication;
- block-quality findings linked to the immutable narrative, claims, evidence,
  and provider audit without changing customer state;
- status-specific obligation, claim, fact, and limitation coverage retained as
  audit and eval inputs.

Gate:

- accepted provider text is retained byte-for-byte;
- accepted typed blocks retain block ID, digest, and original writer-attempt
  provenance;
- writer structure and emphasis are free within the supplied material projection;
- requirement coverage and quality findings are persisted for Workbench and
  human review without writer retry, automatic completion, publication veto,
  customer warning, state downgrade, or delivery delay;
- permissions, sensitive-output safety, SQL/data contracts, evidence and claim
  provenance, persistence integrity, narrative existence, and delivery remain
  hard boundaries;
- no local template supplies a high-value business answer;
- quality-audit findings cannot mutate or withdraw the first publication.

## Phase 6: Publication transaction and delivery

Deliver:

- deterministic `PublicationProjection` proving that it added no fact;
- atomic publication transaction binding bundle, narrative, projection, and
  outbox record;
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
2. run the standard business, runtime, security, browser and real-chain packs
   against isolated threads;
3. verify persisted intent, accepted plan, task, evidence, claim, publication,
   delivery and customer-safe projection closure automatically;
4. generate the release manifest and update current documentation.

Completion requires weak-evidence and missing-contract boundaries, branch
isolation, restart/resume, duplicate dispatch, authority-seal idempotency,
delivery retry, single-analysis parity and fixed safe projection to pass.

## Phase 8: Complete first-answer performance

Deliver:

- node- and Provider-level latency, retry, token and failure attribution in the
  WAJE audit trail;
- compact, lossless Provider projections for intent, semantic settlement and
  narrative generation;
- off-customer-path thread-summary maintenance and post-delivery quality audit;
- one `agent-turn-action-binding.v2` record carrying the exact selected tool and
  canonical typed arguments;
- one real Provider request after an already bound read-only artifact tool,
  while ordinary and multi-round function loops remain SDK-controlled;
- explicit 480-second complete first-answer and 20-second published-material
  follow-up contracts.

Gate:

- the real DeepSeek first answer closes accepted Plan, task, evidence, claim,
  publication and delivery authority within 480 seconds;
- each artifact follow-up finishes within 20 seconds, performs at most one
  bound tool read and cannot start a new BI analysis;
- `OPENAI_API_KEY` is absent, the only outbound model origin is the configured
  mainland Provider and OpenAI hosted request count is zero;
- answer-quality review remains post-delivery advisory and cannot affect the
  gate result, publication state or customer message;
- deterministic Phase 7/8, static contracts, release manifest and deployment
  checks pass from the final source state.

## Current status

Phases 0–8 are the accepted single-authority baseline. The final isolated P8
real-chain acceptance completed the full first answer in 318.835 seconds and
two published-material follow-ups in 11.175/9.518 seconds. The first answer
preserved 21 accepted capability tasks, 23 evidence entries, 22 verified
claims, one publication and one delivered customer payload. Each follow-up
performed one prebound read-only artifact tool execution and one real DeepSeek
generation without rerunning BI.

All six first-answer model calls completed on their first attempt. The
acceptance environment had no `OPENAI_API_KEY`; the only model origin was the
configured DeepSeek endpoint and OpenAI hosted request count was zero. Legal
fact-owner ambiguity is resolved mechanically from accepted authority while
the original narrative text remains unchanged. Completeness, depth,
readability and actionability stay in the post-delivery advisory record and
cannot alter the gate or publication.

Case B and further multi-Agent expansion remain subsequent stages. They do not
change Phase 8 completion or the advisory-only answer-quality decision.
