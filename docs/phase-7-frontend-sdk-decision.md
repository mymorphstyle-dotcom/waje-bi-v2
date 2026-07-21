# Phase 7 Frontend SDK Decision

Date: 2026-07-08
Rebased: 2026-07-18 on the accepted single-authority architecture

## Decision

WAJE keeps its Gateway APIs, PostgreSQL runtime store, business-readable process
events, and fixed customer publication projection as the frontend contract.
21st Agent Elements / `@21st-sdk/react` does not own Phase 7 runtime state.

The frontend consumes persisted stage projections and customer publication
records. It never reconstructs intent, planning decisions, evidence, claims,
verification, or delivery authority from UI state.

## Current authority boundary

The backend owns these immutable records:

```text
IntentRevision + DecisionLedger
AuthorityContext + PlannerProposal + ProposalAdmissionRecord + PlanRevision
CapabilityOutcome + EvidenceLedger
ClaimGraph + sealed AuthorityBundle
NarrativeDocument + block-verifier report + PublicationProjection
delivery outbox
```

The Gateway exposes fixed, customer-safe projections of the relevant records at
business checkpoints such as `planned`, `evidence_ready`, `authority_sealed`,
`narrative_ready`, and `completed`. A later checkpoint references prior durable
records; it cannot rebuild or reinterpret them.

User identity controls conversation ownership, audit, rate limits, and
performance safety. It does not change datasets, snapshots, plans, evidence
ceilings, claims, verifier decisions, or publication strength.

## UI responsibilities

The workbench may:

- render thread and run state, business checkpoints, clarification options,
  admitted plan items, execution outcomes, verified claims, limitations, and
  accepted narrative blocks;
- use `@xyflow/react` to visualize persisted plan and execution relationships;
- present non-sensitive query/result references, contract and snapshot refs,
  completeness, and verifier status;
- retry idempotent delivery or refresh an existing customer projection through
  WAJE-owned Gateway operations.

The workbench may not:

- infer an execution plan from display labels or model prose;
- turn an omitted or failed branch into a zero-impact finding;
- promote candidate evidence into a stronger claim;
- rewrite a `NarrativeDocument` block after verification;
- expose SQL, raw rows, owner/debug fields, secrets, hidden reasoning, or
  unrestricted provider payloads;
- treat local component state as recovery or audit authority.

## SDK boundary

An external component library may be adopted later for chat, composer, process
rows, or visualization ergonomics when it consumes the existing Gateway
contracts through a WAJE-owned adapter. It cannot become a BI truth source,
thread store, memory store, ownership authority, verifier, or delivery ledger.

## Current implementation choice

- Use Vercel AI Elements conversation, message, and prompt-input primitives for
  the normal-user conversation surface through WAJE-owned adapters.
- Keep the custom `/agent-run-workbench` review surface.
- Keep `@xyflow/react` for plan and execution inspection.
- Keep the normal-user page as one continuous conversation timeline; workflow
  canvas and technical lifecycle panels stay in Workbench.
- Keep `@ai-sdk/react` / `ai` available only where their streaming primitives fit
  the persisted Gateway projection.
- Keep unused external agent-runtime SDKs out of production dependencies.

## Replay read-model contract

`GET /api/agent-runs` is the typed, customer-safe read model for the workbench.
It returns one canonical projection per `runId` and keeps execution, verifier,
publication, and delivery states separate.

Once a validated customer publication exists, that projection remains canonical
while delivery is pending or failed. The run stays visible from publication
creation through every delivery outcome; a runtime snapshot cannot replace the
published authority because of an independent read or delivery race.

An `event_replay` is available only when the accepted workflow path has a
complete, monotonic persisted chronology. Provider, query, and capability stage
windows come from the durable call journal; authority sealing, publication, and
delivery use their own persisted timestamps. A run with missing or contradictory
chronology is exposed as a `static_snapshot` with explicit incomplete or unknown
trace state.

Counts and graph fields stay absent when their source was not recorded. The
Gateway does not coerce missing values to zero or an empty graph. Claim-to-
evidence relationships are projected only from persisted claim and evidence
references, and the UI cannot infer a relationship from labels, ordering, or
prose similarity.

The accepted graph is task-granular: every item carries its plan revision, task
identity, task key, capability identity, and a discriminated execution state.
`not_started` means the plan accepted the task and no execution activity exists;
`unsettled` means execution activity exists without an accepted snapshot closure;
neither state is reported as an unknown outcome. A `settled` task carries its
authoritative outcome, retryability, limitation references, and any
customer-safe failure boundary. Settled outcomes preserve the difference
between `succeeded`, `unavailable`, `integrity_failed`, `technical_failed`,
`skipped`, and `superseded`; the UI does not infer task success from the
presence or absence of evidence. The UI renders only the failure's business
boundary and never displays internal technical detail.

Evidence retains its execution state and binds through the durable execution transition,
task attempt, outcome, and ledger entry chain. Task cards and their
evidence use the exact `(planRevisionId, taskId)` identity, so repeated
capabilities and plan-patch execution rounds keep separate evidence and claim
bindings. Provider, model, attempt, and internal audit payloads remain server-side.

## Revisit trigger

Re-evaluate the component choice when streaming or workbench maintenance cost
justifies it and a candidate SDK can render WAJE's persisted customer-safe
records without changing backend authority.
