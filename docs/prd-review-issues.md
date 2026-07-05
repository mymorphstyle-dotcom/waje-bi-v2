# WAJE BI v2 PRD Review Issues

Review target: `docs/prd.md`  
Review date: 2026-07-03  
Backfill status: P0-P2 confirmed and backfilled into `docs/prd.md`  
Scope: product completeness, launch acceptance, PRD-to-implementation readiness

## Summary

The PRD is structurally sound. It captures the eight business question families, the SQL-first boundary, the LangGraph/WAJE split, evidence and claim constraints, and the full-coverage baseline principle.

The issues below were reviewed with the user and backfilled into the Chinese PRD draft. Keep this file as an audit trail for why the new sections exist.

## P0 - Backfilled Signoff Fixes

### P0-1: Launch acceptance matrix is conceptual only

Status: backfilled in `docs/prd.md` section 21.

Reference after backfill: `docs/prd.md` section 21.

The PRD defines matrix axes and state names, but it does not provide a usable matrix skeleton. Signoff still needs at least starter rows for the eight question families, core capability tags, representative SSOT factor groups, allowed claim thresholds, and degraded-path expectations.

Impact: baseline acceptance cannot be judged without re-interpreting the PRD.

Expected fix: add a launch acceptance matrix skeleton with rows for all eight question families and example cells for `business_evidence_state`, `data_contract_state`, allowed claim/evidence type, allowed strength or wording limit, required capabilities, forbidden overclaims, and visual/verifier expectations.

### P0-2: First vertical slice lacks a structured acceptance package

Status: backfilled in `docs/prd.md` section 10.

Reference after backfill: `docs/prd.md` section 10.

The month-start question is described as the first pattern-domain acceptance case, but the PRD does not yet define the structured expectation package for it.

Impact: the most important regression case remains underspecified.

Expected fix: add a dedicated acceptance spec for the first slice covering natural-language input, expected family, required capabilities, forbidden capabilities, 1-10 / 11-20 / 21-end window definition, recurrence/effect/stability criteria, exception handling, expected visual blocks, answer wording boundaries, and verifier pass/fail rules.

### P0-3: Accepted graph state is missing a product-level lifecycle contract

Status: backfilled in `docs/prd.md` section 13.

Reference after backfill: `docs/prd.md` sections 13, 14, and 15.

The PRD repeatedly references accepted graph, compiler repair, degradation, question tool outcomes, and Answer Package assumptions, but it does not define the minimum lifecycle states that must be visible and recorded.

Impact: implementation teams can produce incompatible run-state and UI event models while still appearing PRD-compliant.

Expected fix: add a product contract for graph/node state such as `proposed`, `accepted`, `auto_added`, `targeted_repair_requested`, `repaired`, `running`, `completed`, `degraded`, `blocked`, `skipped`, and `verified`, plus clarification result states such as `user_selected`, `recommended_inference_selected`, `agent_instructed_differently`, and `system_inferred`.

### P0-4: Capability cards are specified as a template, but the eight baseline cards are not sketched

Status: backfilled in `docs/prd.md` section 12.

Reference after backfill: `docs/prd.md` section 12.

The PRD lists fields for capability cards and names the eight capabilities, but it does not include even minimal card sketches for each baseline capability.

Impact: LLM planning, graph compiler lint, and eval expectation packages cannot be derived consistently.

Expected fix: add a one-page baseline card sketch per capability: `pattern_scan`, `formula_decompose`, `joint_attribution`, `event_evidence`, `outlier_scan`, `segment_bridge`, `data_quality_check`, and `answer_verify`.

### P0-5: Answer Package fields are listed, but claim group and verifier contracts are too thin

Status: backfilled in `docs/prd.md` sections 7 and 15.

Reference after backfill: `docs/prd.md` sections 7 and 15.

The PRD says Answer Package includes claim groups, evidence refs, verifier result, and visualization plan. It does not define the minimum claim-group shape needed for first-screen rendering and verifier behavior.

Impact: answer UI, verifier, and eval may disagree on what a supported conclusion is.

Expected fix: define minimum claim group fields at product-contract level: conclusion text, scope, baseline, target metric, evidence refs, evidence type, strength, supported wording, disallowed wording, limitations, related visual blocks, and verifier status.

## P1 - Backfilled Implementation-Planning Fixes

### P1-1: Factor ledger needs a reconciliation workflow artifact

Status: backfilled in `docs/prd.md` section 11.

Reference after backfill: `docs/prd.md` section 11.

The PRD defines ledger concept and dual ownership, but the `.mm` to ledger reconciliation flow is still implicit.

Expected fix: describe the review artifact and workflow: source tree extraction, generated review sheet or review view, owner review, status assignment, mismatch detection, missing-node detection, versioned source update, and runtime mirror publish.

### P1-2: Business object impact naming should be made consistent across answer and visualization language

Status: backfilled in `docs/prd.md` sections 4, 9.3, and 16.

Reference after backfill: `docs/prd.md` sections 4, 9.3, and 16.

The question family was renamed to `business_object_impact_review`, while answer cards and semantic views still use event-oriented language in places. Event-specific terms are still useful as subtypes, but PRD wording should make the broader family obvious.

Backfilled fix: generic family language now uses `business_object_impact_review`; event terms remain only as object or visualization subtypes.

### P1-3: Production requirements need minimum launch gates

Status: backfilled in `docs/prd.md` section 19.

Reference after backfill: `docs/prd.md` section 19.

Permissions, audit, snapshots, rerun, performance, deployment, and observability are stated as product effects. They still lack pass/fail gates.

Expected fix: add minimal launch gates, such as permission-blocked claim behavior, rerun comparability requirement, audit trace completeness, budget skip recording, slow-run visibility, and degraded-run observability.

### P1-4: Question tool UX needs result handling for the escape option

Status: backfilled in `docs/prd.md` section 6.

Reference after backfill: `docs/prd.md` section 6.

The PRD includes the "tell the agent to do differently" option, but it does not explain how the system handles that free-form instruction in graph state and compiler validation.

Expected fix: add a short flow: user override enters targeted repair or intent rebinding, compiler validates the changed graph, rejected instructions produce business-facing explanation, and accepted changes record mutation reason.

### P1-5: Artifact persistence and sharing permissions are absent from the PRD

Status: backfilled in `docs/prd.md` section 17.

Reference after backfill: `docs/prd.md` section 17.

Prior product decisions include reusable/shareable analysis artifacts and permission-filtered artifact access. The PRD currently focuses on live threads and answers.

Expected fix: decide whether artifacts are baseline scope. If yes, add artifact requirements: saved answer, visualization plan, process summary, evidence boundaries, permission-filtered read view, export constraints, and continue-investigation entry point.

### P1-6: Eval failure attribution is not included in the PRD body

Status: backfilled in `docs/prd.md` section 20.

Reference after backfill: `docs/prd.md` section 20.

The PRD describes eval packages and run cadence, but it does not include failure attribution labels by business failure type and system responsibility point.

Expected fix: add failure attribution taxonomy so eval failures map to LLM reasoner, graph compiler, semantic compiler, capability API, evidence reducer, answer synthesizer, answer verifier, or visualization planner.

## P2 - Backfilled Readability And Maintenance Fixes

### P2-1: Decide PRD language for business review

Status: backfilled throughout `docs/prd.md`.

Reference after backfill: `docs/prd.md`.

The PRD is in English with Chinese business examples. If business stakeholders review in Chinese, a Chinese PRD or bilingual executive summary will reduce interpretation drift.

### P2-2: Add a short glossary

Status: backfilled in `docs/prd.md` section 0.

Reference after backfill: `docs/prd.md` section 0.

Terms like accepted graph, capability card, evidence envelope, factor ledger, claim group, and business object impact are central. A short glossary would make review faster.

### P2-3: Link demo explicitly as UX pattern only

Status: backfilled in `docs/prd.md` section 5.

Reference after backfill: `docs/prd.md` section 5.

The PRD says the local demo is a UX pattern reference. Add the local files or route that demonstrate this pattern, while repeating that mock details do not define protocol or architecture.

### P2-4: Add PRD traceability to source decisions

Status: backfilled in `docs/prd.md` section 24.

Reference after backfill: `docs/prd.md` section 24.

The PRD cites `docs/product-decisions.md` globally. For maintainability, major sections could include short source notes or decision ids when the document stabilizes.
