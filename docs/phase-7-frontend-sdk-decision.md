# Phase 7 Frontend SDK Decision

Date: 2026-07-08

## Decision

WAJE Phase 7 keeps `WAJE TraceRun`, Gateway APIs, `Postgres Runtime Store`, LangGraph run nodes, and Answer Package as the runtime-facing frontend contract. 21st Agent Elements / `@21st-sdk/react` is not adopted for Phase 7 runtime.

The current workbench can continue using local React components, `@xyflow/react` for the workflow canvas, and WAJE-owned contracts for thread, run, evidence, claim, clarification, artifact, and memory state.

## Why

WAJE needs a Codex-like investigation shell, but BI truth and auditability must stay in WAJE-owned systems:

- `TraceRun` is already bound to Answer Package, run nodes, evidence refs, claim groups, visual blocks, and role-filtered artifact behavior.
- The Gateway APIs already expose thread, run events, clarifications, artifact continue/open/export, and memory proposal decisions.
- `Postgres Runtime Store` owns product state, audit events, permissions, result refs, evidence refs, answer packages, and memory proposal records.
- LangGraph remains the execution/checkpoint/trace adapter; frontend components should render its business-readable process, not redefine it.
- External UI/runtime SDKs may help with component ergonomics later, but they cannot become a BI truth source, memory source, or permission/audit authority.

## Boundary

Allowed later:

- Evaluate 21st-style components, Vercel AI Elements, assistant-ui, or a small internal component kit for chat, composer, tool cards, and streaming rows.
- Adopt a component library only if it consumes WAJE's existing contracts without changing backend authority.
- Wrap external components behind WAJE-owned adapters so the product contract stays stable.

Not allowed in Phase 7 runtime:

- Moving thread/topic/run state from Postgres into an external SDK runtime.
- Letting a frontend SDK decide evidence strength, result reuse, claim support, permission filtering, or memory persistence.
- Letting SDK tool calls reach raw SQL or bypass WAJE capability APIs.
- Sending raw LLM payloads, hidden reasoning, SQL, evidence envelopes, or verifier internals to the default business UI.

## Current Implementation Choice

- Keep the custom `/agent-run-workbench` route as the Phase 7 review surface.
- Keep `@xyflow/react` for the wide workflow canvas because the current requirement is graph inspection, highlighted path, accepted graph, bypasses, and node inspector behavior.
- Keep `@ai-sdk/react` / `ai` available for future chat streaming work where it fits the Gateway API shape.
- Remove unused `@21st-sdk/react` from production dependencies until a later PR adopts it behind a WAJE adapter.

## Revisit Trigger

Re-evaluate the SDK choice when one of these is true:

- The chat composer becomes live streaming and needs a richer message/tool-call component model.
- The current workbench component cost starts slowing feature work.
- A candidate SDK can render WAJE thread/run/process/evidence contracts directly, with permission-safe lazy loading and no runtime authority shift.
- Phase 8 production gates require integration-level observability or deployment features that an external SDK can provide without weakening WAJE's evidence boundary.
