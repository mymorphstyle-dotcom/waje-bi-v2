# WAJE BI v2 Documentation

## Product and delivery

- [Product requirements](./prd.md)
- [Historical product decision log](./product-decisions.md)
- [Implementation roadmap](./implementation-roadmap.md)
- [Next optimization: answer completeness and conversation readability](./superpowers/plans/2026-07-22-answer-completeness-and-conversation-readability.md)
- [P4 full-factor investigation and controlled orchestration](./superpowers/plans/2026-07-23-p4-full-factor-investigation-orchestration.md)
- [P5 data coverage and performance](./superpowers/plans/2026-07-23-p5-data-coverage-performance.md)
- [P6 production-chain acceptance](./superpowers/plans/2026-07-23-p6-production-chain-acceptance.md)
- [P7 answer-completeness repair](./superpowers/plans/2026-07-23-p7-answer-completeness-repair.md)
- [P9 Case B and controlled multi-Agent investigation](./superpowers/plans/2026-07-24-p9-case-b-controlled-multi-agent.md)
- [Failure attribution taxonomy](./evals/failure-attribution-taxonomy.md)

## Runtime architecture

- [General Agent Runtime production hardening ledger](./reviews/2026-07-22-production-hardening.md)
- [General Agent Runtime P3 target-environment delivery ledger](./reviews/2026-07-22-p3-target-environment-delivery.md)
- [WAJE BI v2 P4 full-factor delivery report](./reviews/2026-07-23-p4-full-factor-delivery.md)
- [First-payment and timezone data-quality closeout](./reviews/2026-07-23-first-payment-timezone-data-quality.md)
- [P5 data coverage and performance report](./reviews/2026-07-23-p5-data-coverage-performance.md)
- [P6 production-chain acceptance report](./reviews/2026-07-23-p6-production-chain-acceptance.md)
- [P7 answer-completeness repair report](./reviews/2026-07-23-p7-answer-completeness-repair.md)
- [P8 complete first-answer performance report](./reviews/2026-07-24-p8-first-answer-performance.md)
- [General Agent Runtime target architecture](./specs/general-agent-runtime/target-architecture.md)
- [General Agent Runtime P0 framework and provider](./specs/general-agent-runtime/p0-framework-provider.md)
- [General Agent Runtime P0 conversation and state authority](./specs/general-agent-runtime/p0-conversation-state-authority.md)
- [General Agent Runtime P0 existing material explanation](./specs/general-agent-runtime/p0-existing-material-explanation.md)
- [General Agent Runtime P1 BI analysis tool boundary](./specs/general-agent-runtime/p1-bi-analysis-tools.md)
- [General Agent Runtime P1 durable tool supervision](./specs/general-agent-runtime/p1-durable-tool-supervision.md)
- [General Agent Runtime P1 transport cutover](./specs/general-agent-runtime/p1-transport-cutover.md)
- [General Agent Runtime P2 context and controlled delegation](./specs/general-agent-runtime/p2-context-and-delegation.md)
- [General Agent Runtime P3 deployment acceptance](./specs/general-agent-runtime/p3-deployment-acceptance.md)
- [WAJE Standard Pack v1](./specs/general-agent-runtime/standard-pack-v1.md)
- [Accepted single-authority workflow](./adr/2026-07-17-single-authority-agent-workflow.md)
- [Retired Phase 4 workflow reference](./phase-4-agent-workflow-reference.md)
- [Architecture decision index](./adr/README.md)
- [Live conversation acceptance](./phase-7-live-conversation-eval.md)
- [Frontend SDK boundary](./phase-7-frontend-sdk-decision.md)

The 2026-07-17 single-authority ADR and the 2026-07-20 advisory-publication ADR
are the current architecture authority. `IntentRevision`,
`DecisionLedger`, `AuthorityContext`, `PlannerProposal`, deterministic admission,
`PlanRevision`, `CapabilityOutcome`, `EvidenceLedger`, `ClaimGraph`,
`AuthorityBundle`, `NarrativeMaterialProjection`, `NarrativeDocument`,
`PublicationFlow`, `PublicationProjection`, and the delivery outbox form the only
current workflow contract. The material projection carries opaque
`user_required` publication requirements while preserving writer expression
freedom within evidence and claim boundaries. Accepted intent and plan semantics
are projected to the writer as typed business context; raw customer text and
fixed-sensitive identifiers stay outside provider payloads. Subjective quality,
explanation-depth, readability, actionability and potential-hallucination findings
enter the Workbench review chain after delivery. They do not block publication or
trigger an automatic rewrite. The typed structural-completeness assessment is also
an advisory record: it cannot add a narrative revision, remove original text, delay
delivery, create a customer warning or alter publication state. Retired references are
kept only to explain superseded implementation history. Live acceptance remains
governed by the implementation roadmap and real-conversation protocol.

The General Agent Runtime framework/provider, continuous state authority,
existing-material explanation, BI analysis tool submission, and durable tool
supervision boundaries are
implemented behind Python WAJE contracts. The adapter runs the OpenAI Agents
SDK with an explicit mainland model provider over Chat Completions, replaces
the default trace exporter with a WAJE sink, and rejects SDK model defaults and
OpenAI endpoints. The runtime uses one persisted `ThreadItemLedger`, reads only
publication-reachable customer-safe analysis artifacts, and can explain
published claims and scores without starting a new BI analysis. New analysis
and material revision tools submit the existing recoverable
`analysis_runs + run_dispatches` workflow. Long tools stop the in-process SDK
loop after submission, persist an SDK-neutral checkpoint in the same thread ledger,
preserve the background task in ThreadHead, and recover completed customer-safe
publications through a typed completion loader. IntentRevision, PlanRevision,
LangGraph, evidence, claim, publication, and delivery remain the only BI
authority chain. The normal message route now starts the General Agent without
pre-creating a BI run. Production recovery scans terminal BI tasks into an
idempotent Agent resume outbox, and thread SSE carries an independent event
cursor alongside ThreadHead state version and item sequence. Refresh,
disconnect, close-page, stale-cursor, pending-action, and multi-tab browser
acceptance are covered by the current Playwright suite. P1 transport and P2 context/delegation
are closed. P3 provides one deployment gate for schema v13, the live mainland Provider path,
P2 runtime smoke, outbound restrictions, and WAJE-only trace evidence.

P4 closes the full-factor BI investigation boundary. The accepted plan now compiles
a content-addressed `FactorCoveragePlan` across ten reviewed business domains,
including acquisition/registration/first-payment funnels, amount-tier mix, payment
chain, gameplay, operations, calendar/payday, internal events, external context,
and data quality. Branch results settle independently, replay from PostgreSQL,
bind to verified claims, and expose full topology only in Workbench. Missing event
matches or unsupported contracts remain observable limitations; they never become
an assertion that a factor had no impact. Publication quality review remains a
post-delivery human advisory process.

P7 closes the structural answer-completeness loop. Accepted factor requests become
scoped user-required obligations; payment final-outcome evidence reconciles
full-sample totals across payment method and channel, publishes baseline/target facts,
and carries representative dimension summaries under a typed interpretation contract.
Execution evidence gaps continue through the existing bounded `PlanPatch` path before
claim settlement. The writer creates one first-publication narrative; completeness,
depth and wording findings are saved after delivery without a completion revision,
automatic rewrite, warning or state downgrade. A persisted customer-safe read-only
tool result can close a follow-up as `completed_with_limits` when only the final model
synthesis fails; tools must opt in through a typed recovery contract. If model-owned
dynamic tool selection is temporarily unavailable, a thread with a customer-safe
`bi_publication` preserves the latest complete publication and clearly marks the new
follow-up interpretation as unfinished. Contract, context and source-closure failures
continue to fail closed.

P8 closes the current first-answer performance gate. Compact Provider projections,
off-path thread-summary maintenance, one-call read-only follow-ups and deterministic
authority reference assembly reduce the real complete first answer to 318.835 seconds;
two published-material follow-ups complete in 11.175 and 9.518 seconds. The accepted
Plan still carries 21 capability tasks and settles 23 evidence entries and 22 verified
claims. The run uses only the configured DeepSeek Chat Completions endpoint, with no
`OPENAI_API_KEY` and zero OpenAI hosted requests. Answer-quality review remains
post-delivery advisory.

Case B and further multi-Agent expansion remain subsequent work. Manual truth review,
manual insight scoring and wording-pair review remain advisory evaluation surfaces.
