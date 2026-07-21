# Retired Phase 4 Agent Workflow Reference

Status: retired on 2026-07-18.

This document no longer defines current WAJE BI v2 behavior. The former Phase 4
workflow combined planning, evidence reduction, answer construction, verification,
and frontend delivery through mutable graph state. That authority model was
removed during the no-compatibility single-authority cutover.

The current architecture is defined by
[2026-07-17: Single-authority agent workflow convergence](./adr/2026-07-17-single-authority-agent-workflow.md).
Current implementation and acceptance work must use these records:

```text
IntentRevision
→ DecisionLedger
→ AuthorityContext
→ PlannerProposal + ProposalAdmissionRecord
→ PlanRevision
→ CapabilityOutcome + EvidenceLedger
→ ClaimGraph
→ sealed AuthorityBundle
→ NarrativeDocument
→ PublicationProjection
→ delivery outbox
```

This filename remains as a historical pointer so old links fail visibly into a
retired notice. It provides no compatibility contract, fallback route, schema,
or acceptance authority.
