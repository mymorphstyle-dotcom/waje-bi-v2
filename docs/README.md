# WAJE BI v2 Documentation

## Product and delivery

- [Product requirements](./prd.md)
- [Historical product decision log](./product-decisions.md)
- [Implementation roadmap](./implementation-roadmap.md)
- [Failure attribution taxonomy](./evals/failure-attribution-taxonomy.md)

## Runtime architecture

- [General Agent Runtime target architecture](./specs/general-agent-runtime/target-architecture.md)
- [General Agent Runtime P0 framework and provider](./specs/general-agent-runtime/p0-framework-provider.md)
- [General Agent Runtime P0 conversation and state authority](./specs/general-agent-runtime/p0-conversation-state-authority.md)
- [General Agent Runtime P0 existing material explanation](./specs/general-agent-runtime/p0-existing-material-explanation.md)
- [General Agent Runtime P1 BI analysis tool boundary](./specs/general-agent-runtime/p1-bi-analysis-tools.md)
- [General Agent Runtime P1 durable tool supervision](./specs/general-agent-runtime/p1-durable-tool-supervision.md)
- [General Agent Runtime P1 transport cutover](./specs/general-agent-runtime/p1-transport-cutover.md)
- [General Agent Runtime P2 context and controlled delegation](./specs/general-agent-runtime/p2-context-and-delegation.md)
- [Accepted single-authority workflow](./adr/2026-07-17-single-authority-agent-workflow.md)
- [Retired Phase 4 workflow reference](./phase-4-agent-workflow-reference.md)
- [Architecture decision index](./adr/README.md)
- [Live conversation acceptance](./phase-7-live-conversation-eval.md)
- [Frontend SDK boundary](./phase-7-frontend-sdk-decision.md)

The 2026-07-17 ADR is the current architecture authority. `IntentRevision`,
`DecisionLedger`, `AuthorityContext`, `PlannerProposal`, deterministic admission,
`PlanRevision`, `CapabilityOutcome`, `EvidenceLedger`, `ClaimGraph`,
`AuthorityBundle`, `NarrativeMaterialProjection`, `NarrativeDocument`,
`PublicationFlow`, `PublicationProjection`, and the delivery outbox form the only
current workflow contract. The material projection carries opaque
`user_required` publication requirements while preserving writer expression
freedom within evidence and claim boundaries. Focused repair sends only rejected
targets back to the writer; runtime preserves accepted typed block provenance
and performs the mixed-origin revision merge. Retired references are kept only
to explain superseded implementation history. Live acceptance remains governed
by the implementation roadmap and real-conversation protocol.

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
acceptance are covered by the current Playwright suite. P1 transport is closed;
context compaction and controlled multi-Agent work remain in P2.

The launch gate is fully automated except for one fresh post-freeze Case B run
through the real service chain. Manual truth review, manual insight scoring, and
wording-pair review are optional post-launch evaluations.
