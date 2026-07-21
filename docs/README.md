# WAJE BI v2 Documentation

## Product and delivery

- [Product requirements](./prd.md)
- [Historical product decision log](./product-decisions.md)
- [Implementation roadmap](./implementation-roadmap.md)
- [Failure attribution taxonomy](./evals/failure-attribution-taxonomy.md)

## Runtime architecture

- [General Agent Runtime target architecture](./specs/general-agent-runtime/target-architecture.md)
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

The General Agent Runtime document is the next implementation target for the
conversation entry, continuous follow-up, durable task, and customer thread
projection. Its framework decision is accepted: the Python top-level loop uses
the OpenAI Agents SDK with an explicit mainland-model provider over Chat
Completions; the existing LangGraph single-authority workflow runs inside BI
tools; PostgreSQL remains the conversation, task, checkpoint, and artifact
authority. OpenAI-hosted models, state, tools, and tracing are outside the
runtime path. The document does not describe completed behavior and does not
supersede the accepted BI workflow until its implementation and acceptance
gates pass.

The launch gate is fully automated except for one fresh post-freeze Case B run
through the real service chain. Manual truth review, manual insight scoring, and
wording-pair review are optional post-launch evaluations.
