# WAJE BI v2 Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the signed PRD into a production baseline implementation sequence for WAJE BI v2.

**Architecture:** WAJE-owned contracts, compiler, capability APIs, evidence ledger, permissions, and verifier hold BI truth. LangGraph carries visible execution, checkpointing, loops, repairs, and process events. The first vertical slice proves the generalized `pattern_explanation` problem class, with the month-start payment pattern as its first regression case.

**Tech Stack:** TypeScript frontend/gateway, Python BI Agent Core, LangGraph adapter, Postgres runtime mirror, ClickHouse analytical query access, versioned contract source files.

---

## Principles

- Build a production baseline with full launch coverage.
- Ship by business capability, with evidence, verifier, eval, and UI visibility included in each slice.
- Keep table structures and final API schemas in technical design, while this roadmap defines build order and acceptance.
- Treat recipe entries as starting templates. The accepted graph is compiled from user intent, contracts, evidence needs, policy, permissions, and budget.
- Keep the first slice generic to same-class pattern questions. The month-start case is a regression example for the broader pattern workflow.

## Phase 0: Signoff Baseline

**Goal:** Freeze the product contract before implementation starts.

**Business reason:** Engineering should build against one reviewed baseline, so implementation does not drift into demo behavior or old WAJE reuse.

**Deliverables:**

- [ ] `docs/prd.md` remains at `v0.1 signoff draft`.
- [ ] `docs/product-decisions.md` contains all PRD signoff decisions.
- [ ] Implementation work references PRD sections for question families, evidence states, graph compiler, launch eval, and launch gates.

**Acceptance:**

- [ ] No unresolved P0/P1 PRD review findings.
- [ ] Legacy event-impact naming is absent from active PRD paths.
- [ ] Ledger states match `data_contract_state`: `contract_backed`, `evidence_linked`, `static_assumption`, `missing_contract`, `permission_limited`, `unsupported_grain`, `out_of_scope_for_now`.

## Phase 1: Contract Foundation

Detailed breakdown: [docs/phase-1-contract-foundation-breakdown.md](/Users/luka/work/waje-bi-v2/docs/phase-1-contract-foundation-breakdown.md:1)

**Current progress (2026-07-05):** Phase 1 has a reviewable contract loop for CF-04 through CF-10 and the first real paid-order source contract is accepted for the 2026-01-01 through 2026-06-30 snapshot. Factor ledger, capability support, missing-contract backlog, dimension/event/assumption sources, eight capability cards, factor review artifact, real-data intake review, `ruby tools/contracts/validate-contracts.rb`, and the accepted initial 142-node SSOT reconciliation map are in place. The latest source coverage pass added profiled source contracts for 大盘 2024-01-01 through 2026-06-02, gameplay overall/by-channel 2024-01-01 through 2026-06-02, old WAJE external events through 2026-06-08, and competitor ranking through 2026-06-07. Dev Postgres contract mirror is initialized. Remaining Phase 1 work is limited to explicitly tracked gaps such as refund/reversal source and gameplay payment attribution.

**Goal:** Make the SSOT and runtime contracts reviewable before query execution.

**Business reason:** Analysts need to know which factors can support strong claims, weak candidate explanations, missing-contract limitations, or no claim.

**Deliverables:**

- [x] Versioned metric contracts for `付费金额` and required formula paths.
- [x] Versioned dimension, event, static-assumption, and missing-contract contract sources.
- [x] Factor ledger review artifact generated from `付费金额影响因子分析.mm`.
- [x] Factor ledger support records by factor, question family, capability, grain, and claim type.
- [x] Capability card source for all eight baseline capabilities.

**Acceptance:**

- [ ] Every relevant SSOT node has a ledger status.
- [x] Payday is represented as a universal 25..30 event-window dimension and candidate mechanism.
- [x] Unsupported grains and missing contracts appear as explicit backlog or limitation records.
- [x] Capability cards expose required parameters, optional parameters, evidence payload, lint severity, degradation output, and verifier hooks.

## Phase 2: Core Runtime Contract

**Goal:** Build the minimal WAJE-owned runtime needed to accept, reject, repair, degrade, and audit analysis graphs.

**Business reason:** LLM planning is useful only when local systems decide what can run and what claims can be made.

**Deliverables:**

- [ ] `analysis_graph` product contract with node fields from the PRD.
- [ ] Graph compiler action table implemented for block, auto-add, degrade, targeted repair, skip, verifier repair, and human-reviewed failure promotion.
- [ ] Evidence envelope contract with common audit and verifier fields.
- [ ] Run state records linked to LangGraph node ids.
- [ ] Permission, contract-version, completeness, timezone, and cumulative-value guards.

**Acceptance:**

- [ ] LLM can propose a candidate capability graph but cannot execute raw SQL.
- [ ] Compiler records accepted, auto-added, repaired, degraded, blocked, and skipped paths.
- [ ] Every run pins contract versions and current-data snapshot or freshness marker.
- [ ] User-visible process events use business language.

## Phase 3: Semantic Query And Evidence Layer

**Goal:** Let capability APIs compile semantic requests into validated analytical queries and evidence.

**Business reason:** Business conclusions need reproducible numbers, not free-form SQL or hidden notebook logic.

**Deliverables:**

- [ ] Semantic request model for capability calls.
- [ ] Query compiler boundary for ClickHouse analytical access and Postgres runtime mirror access.
- [ ] Evidence persistence with `evidence refs`, result refs, query refs, quality flags, and limitations.
- [ ] Shared semantic query planning for compatible scope, metric, window, grain, filter, and baseline needs.

**Acceptance:**

- [ ] Capability APIs return evidence envelopes plus typed payloads.
- [ ] Query execution is blocked when metric, grain, filter, window, permission, or contract legality fails.
- [ ] Reused results validate scope, filters, grain, metric, window, baseline, contract version, and freshness.

## Phase 4: First Pattern Vertical Slice

**Goal:** Ship the first end-to-end generalized `pattern_explanation` slice.

**Business reason:** The system must correctly recognize, prove, quantify, explain, and verify recurring business patterns without hard-coding the month-start case.

**Deliverables:**

- [ ] Intent binding for intra-period, weekly, event-relative, rolling, lag/recovery, and custom-baseline pattern families.
- [ ] `data_quality_check`, `pattern_scan`, `formula_decompose`, `event_evidence`, `segment_bridge` or `joint_attribution`, `outlier_scan`, and `answer_verify` wired as required evidence paths for launch pattern cases.
- [ ] Month-start regression case for 2024-01 to 2026-05 with 1-10 / 11-20 / 21-end windows.
- [ ] Pattern visual blocks: phase comparison, exception list, formula contribution, candidate explanation ranking, evidence boundary.
- [ ] Answer Package with supported explanations, local/exception explanations, insufficient or ruled-out paths.

**Acceptance:**

- [ ] The month-start question routes to full-sample intra-period pattern analysis.
- [ ] It does not route to ordinary period-over-period attribution, cost-period analysis, or cumulative-value analysis.
- [ ] Pattern claim follows coverage, direction consistency, uplift, stability, exception, downgrade, and data-quality rules.
- [ ] Payday, holidays, activities, and other mechanisms stay within their evidence strength.
- [ ] Same evidence-path shape can run a weekly or event-relative pattern case by swapping windows and candidate mechanisms.

## Post-Phase 4 Rebaseline

Date: 2026-07-07

Current evidence: Phase 4 full-period retest has 10 live ClickHouse cases with 3 passed, 7 degraded, 0 blocked, and 0 failed. The pattern slice is runnable, Answer Package drafts exist, final summaries include evidence numbers, and replay has enough audit material for development review.

Future phases should follow this order:

1. **Phase 5: Answer safety and eval gates.** Harden claim groups, verifier behavior, implicit clarification, failure attribution, and route-drift measurement on the existing pattern slice.
2. **Phase 6: Question-family expansion.** Add the remaining question families only after Phase 5 can classify wrong intent, wrong baseline, weak evidence, route drift, and unsupported claims without manual log reading.
3. **Phase 7: Frontend agent shell.** Build user-facing investigation UX on top of stable Answer Package and replay semantics.
4. **Phase 8: Production gates.** Add release-grade observability, permissions, rerun comparability, rollback, and health checks after the workflow and UI contract are stable.

Deliberate deferrals:

- Route drift between `compare_periods`, `compare_period_phases`, and `rolling_window_compare` remains measured in Phase 5 before adding deterministic compiler rules.
- Broad composite-intent handling belongs to Phase 6. Phase 5 only tests whether latent ambiguity should trigger clarification.
- Production dashboards and release operations stay in Phase 8.

## Phase 5: Answer Package, Verifier, And Eval

Current plan: [docs/superpowers/plans/2026-07-07-phase-5-from-phase-4-state.md](/Users/luka/work/waje-bi-v2/docs/superpowers/plans/2026-07-07-phase-5-from-phase-4-state.md:1)

Closeout evidence (2026-07-08): Phase 5 is complete as an engineering milestone. The latest real ClickHouse + real LLM node-by-node run is `artifacts/phase-5/live-node-system/20260707-v31-prompt-audit-r2/`, with 4 representative cases passed, 81 workflow nodes completed, and 47 LLM calls recorded. Local validation passes with `python3 tools/phase5/validate_phase5.py`, `npm run build`, and `git diff --check`.

**Goal:** Make final answers auditable and regression-tested.

**Business reason:** Production BI answers need clear claim boundaries, visible evidence strength, and repeatable acceptance.

**Deliverables:**

- [x] Claim group contract implemented and emitted in Answer Package summary.
- [x] Answer verifier blocks unsupported strong claims and records visible limitations.
- [x] Independent Causal Auditor LLM reviews causal implications and mechanism hypotheses from a structured evidence dossier.
- [x] Local verifier remains a mechanical evidence checker for refs, numbers, scope, permissions, metric contracts, and auditor wording boundary.
- [x] Launch eval harness uses real user wording plus structured expectation packages.
- [x] Failure attribution labels include business failure type and system responsibility point.
- [x] Implicit clarification eval suite covers latent ambiguity that can change claim quality.
- [x] Route drift measurement records observed drift and impact without auto-promoting guardrails.

**Acceptance:**

- [x] Strong claims cannot publish when verifier fails.
- [x] Eval case fields include allowed claim/evidence type and allowed strength or wording limit.
- [x] Eval failures do not auto-promote into guardrails without human review and dual ownership.
- [x] Smoke, affected-slice, and full acceptance eval runs are defined.

Phase 5 closeout does not claim production release readiness. Broader composite intent coverage, full question-family expansion, production observability, and release gates continue in Phase 6 through Phase 8.

## Phase 6: Capability Expansion Across Eight Question Families

Current plan: [docs/superpowers/plans/2026-07-08-phase-6-question-family-expansion.md](/Users/luka/work/waje-bi-v2/docs/superpowers/plans/2026-07-08-phase-6-question-family-expansion.md:1)

Closeout evidence (2026-07-08): Phase 6 is ready to close as an engineering milestone. The latest real ClickHouse + real LLM run is `artifacts/phase-6/live-question-family/20260708-r9/`, with 12 representative cases passed, 231 workflow nodes completed, and 136 LLM calls recorded. Audit notes are in [docs/reviews/phase6-live-question-family-audit-20260708.md](/Users/luka/work/waje-bi-v2/docs/reviews/phase6-live-question-family-audit-20260708.md:1).

**Goal:** Expand from the first pattern slice into the full baseline question-family matrix.

Phase 6 starts only after Phase 5 eval gates can classify wrong intent, wrong baseline, weak evidence, route drift, and unsupported claims without manual log reading.

**Business reason:** Paid amount impact and retrospective questions are multi-capability workflows, so launch needs all eight families represented end to end.

**Build order:**

- [x] `paid_amount_change_explanation`: operating-review spine, formula decomposition, attribution, anomaly, pattern, business object evidence, data quality, verifier.
- [x] `business_object_impact_review`: object binding, object-specific evidence route, comparison/control, candidate impact, claim limits.
- [x] `segment_or_factor_attribution`: one-dimensional screening, two-dimensional combination start, higher-order promotion loop, sparse and scope limits.
- [x] `revenue_health_review`: trend, target, structure, funnel/formula, anomaly, data quality, risk wording.
- [x] `anomaly_or_black_swan_review`: pseudo-anomaly rejection, local segment anomaly, metric-chain anomaly, internal/external candidate explanations.
- [x] `custom_baseline_comparison`: user baseline, recommended baseline, multiple-baseline disagreement, comparability checks.
- [x] `data_quality_or_evidence_review`: trust judgment, affected claims, degradation, contract and permission fixes.

**Acceptance:**

- [x] Every question family has at least one end-to-end representative case.
- [x] Launch acceptance matrix covers representative SSOT factor groups and ledger states for every family.
- [x] `answer_verify` runs for launch representative cases.
- [x] `data_quality_check` runs for first-screen claims and strong claims.

## Phase 7: Frontend Agent Shell

**Goal:** Build the user-facing Codex-like investigation experience.

**Business reason:** Users need to see what the agent understood, what it checked, where evidence degraded, and what answer is safe to trust.

Current progress (2026-07-08): Phase 7 runtime foundation now has conversation contracts, Postgres conversation schema, Python Postgres store with audit writes, production-safe TypeScript gateway store selection, thread/topic/turn/run context assembly, result reuse decisions, memory proposals, Python Agent Core bridge, and the public gateway routes for threads, messages, run events, clarifications, artifact continue, artifact read/export, and memory proposal accept/reject. Answer Package now includes a validated `visualization_plan` derived from verified claim groups, and the workbench renders those visual blocks through the existing TraceRun UI contract. Clarification answers now bind back to the pending topic, clear pending state, record the clarification outcome, and resume the same run through Agent Core. Runtime question tool now blocks ambiguous runs with a structured clarification payload, recommended inference, `tell the agent to do differently`, pending clarification state, and run-level audit events; no-answer runs waiting for clarification now appear in the agent workbench as business-readable process traces. Run event streams now include a business-facing `process` summary while retaining raw payloads for audit/debug; persisted run nodes are also emitted as business process events for intent, accepted plan, capability progress, repair/degrade/block, and verifier stages, and completed Postgres Answer Package runs now inject recorded run nodes into the workbench timeline. The ordinary workbench hides raw audit JSON by default, with debug audit available only through an explicit `processSummary.debugAudit` gate. Artifact access now persists refs in Postgres, validates role visibility before open/continue/export, records allow/block/open/export audit events, and passes claim support only when result refs, snapshot, contract, and permission checks match. Coverage is tracked in `evals/phase7/conversation_scenarios.yaml` with 60 natural-language multi-turn scenarios.

**Deliverables:**

- [x] TypeScript gateway routes for thread creation, message submission, run event stream handles, clarifications, artifact continue, and memory proposal decisions.
- [x] Conversation runtime contracts for `Thread`, `Topic`, `Turn`, `Run`, `ContextManifest`, `ReuseDecision`, `MemoryItem`, and `MemoryProposal`.
- [x] Gateway-triggered Python Agent Core bridge that records conversation intent, context manifest, LangGraph result, Answer Package, run nodes, and audit events.
- [x] Answer Package emits validated visualization plan blocks from verified claim groups.
- [x] Agent run workbench consumes persisted Postgres Answer Packages through the existing TraceRun contract.
- [x] Clarification answer resume path with pending-topic binding, outcome audit, and same-run Agent Core execution.
- [x] Continue investigation from saved artifacts with Postgres artifact refs, permission filtering, audit events, and shared Agent Core execution.
- [x] Artifact read-only open and Markdown export with role-filtered Answer Package sections and audit records.
- [x] SDK decision for 21st Agent Elements or a better-fitting alternative.
- [x] Process event rendering for intent, accepted plan, capability progress, question tool, repair/degrade/block/skip, evidence summary, verifier result.
- [x] Dynamic first-screen answer cards from verified claim groups and validated visualization plan.
- [x] Artifact read-only sharing, permission-filtered section rendering, and static export.
- [x] Replace the in-memory-only gateway/runtime store handoff with Postgres persistence and audit writes; development fallback remains local-only.

**Acceptance:**

- [x] Conversation scenario suite covers at least 60 natural-language cases across follow-up, mixed questions, off-topic/tool/unsupported inputs, permissions/snapshots/memory, correction/challenge/clarification.
- [x] Frontend renders WAJE-owned Answer Package and visualization plan.
- [x] Frontend does not infer business truth from raw evidence payloads.
- [x] Question tool supports up to 3-4 short questions, up to 3 options each, recommended inference, and `tell the agent to do differently`.
- [x] Technical internals stay out of ordinary UI while audit/debug details remain available.

## Phase 8: Production Gates

**Goal:** Make launch behavior observable, auditable, repeatable, and permission-safe.

**Business reason:** BI claims affect business decisions, so failures must be visible and reruns must be explainable.

**Deliverables:**

- [ ] Permission-blocked claim behavior and permission-filtered artifacts.
- [ ] Audit trace from answer to run, contract version, evidence, claim, verifier result, and query refs.
- [ ] Snapshot and rerun comparability rules.
- [ ] Capability timeout, row/result budget, and degradation behavior.
- [ ] Health checks for frontend/gateway, Python BI Agent Core, Postgres runtime mirror, ClickHouse access, and LangGraph adapter.
- [ ] Version rollback for contract, ledger, capability card, prompt/recipe, and verifier policy.
- [ ] Launch dashboard for slow run, capability error, compiler block, verifier failure, permission spike, contract mismatch, and ledger mismatch.

**Acceptance:**

- [ ] Slow, failed, degraded, blocked, and verifier-failed runs are locatable.
- [ ] Budget skips enter Answer Package limitations or follow-up.
- [ ] Reruns state whether they are comparable to the original run.
- [ ] Release candidate runs full acceptance eval.

## Implementation Order Summary

1. Phase 0: Signoff baseline.
2. Phase 1: Contract foundation.
3. Phase 2: Core runtime contract.
4. Phase 3: Semantic query and evidence layer.
5. Phase 4: First generalized pattern vertical slice.
6. Phase 5: Answer Package, verifier, and eval.
7. Phase 6: Remaining question families and capability expansion.
8. Phase 7: Frontend shell and artifacts.
9. Phase 8: Production gates.

## First Execution Cut

Start with Phases 1-4 as one implementation program. It creates a focused production-shaped path for the first pattern class case while preserving the full baseline architecture. Phases 5-8 then harden answer safety, broaden question coverage, expose the investigation UI, and meet launch gates.
