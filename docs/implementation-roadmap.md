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

## Phase 5: Answer Package, Verifier, And Eval

**Goal:** Make final answers auditable and regression-tested.

**Business reason:** Production BI answers need clear claim boundaries, visible evidence strength, and repeatable acceptance.

**Deliverables:**

- [ ] Claim group contract implemented for conclusion text, scope, baseline, target metric, evidence refs, evidence type, strength, supported wording, disallowed wording, limitations, related visual blocks, and verifier status.
- [ ] Answer verifier checks numbers, scope, baseline, evidence refs, wording, disabled/degraded paths, and visual blocks.
- [ ] Launch eval harness using real user wording plus structured expectation packages.
- [ ] Failure attribution labels for business failure type and system responsibility point.

**Acceptance:**

- [ ] Strong claims cannot publish when verifier fails.
- [ ] Eval case fields include allowed claim/evidence type and allowed strength or wording limit.
- [ ] Eval failures do not auto-promote into guardrails without human review and dual ownership.
- [ ] Smoke, affected-slice, and full acceptance eval runs are defined.

## Phase 6: Capability Expansion Across Eight Question Families

**Goal:** Expand from the first pattern slice into the full baseline question-family matrix.

**Business reason:** Paid amount impact and retrospective questions are multi-capability workflows, so launch needs all eight families represented end to end.

**Build order:**

- [ ] `paid_amount_change_explanation`: operating-review spine, formula decomposition, attribution, anomaly, pattern, business object evidence, data quality, verifier.
- [ ] `business_object_impact_review`: object binding, object-specific evidence route, comparison/control, candidate impact, claim limits.
- [ ] `segment_or_factor_attribution`: one-dimensional screening, two-dimensional combination start, higher-order promotion loop, sparse and scope limits.
- [ ] `revenue_health_review`: trend, target, structure, funnel/formula, anomaly, data quality, risk wording.
- [ ] `anomaly_or_black_swan_review`: pseudo-anomaly rejection, local segment anomaly, metric-chain anomaly, internal/external candidate explanations.
- [ ] `custom_baseline_comparison`: user baseline, recommended baseline, multiple-baseline disagreement, comparability checks.
- [ ] `data_quality_or_evidence_review`: trust judgment, affected claims, degradation, contract and permission fixes.

**Acceptance:**

- [ ] Every question family has at least one end-to-end representative case.
- [ ] Launch acceptance matrix covers representative SSOT factor groups and ledger states for every family.
- [ ] `answer_verify` runs for launch representative cases.
- [ ] `data_quality_check` runs for first-screen claims and strong claims.

## Phase 7: Frontend Agent Shell

**Goal:** Build the user-facing Codex-like investigation experience.

**Business reason:** Users need to see what the agent understood, what it checked, where evidence degraded, and what answer is safe to trust.

**Deliverables:**

- [ ] TypeScript frontend/gateway with thread UI and streaming gateway.
- [ ] SDK decision for 21st Agent Elements or a better-fitting alternative.
- [ ] Process event rendering for intent, accepted plan, capability progress, question tool, repair/degrade/block/skip, evidence summary, verifier result.
- [ ] Dynamic first-screen answer cards from verified claim groups and validated visualization plan.
- [ ] Artifact save, read-only sharing, permission-filtered access, static export, and continue-investigation entry.

**Acceptance:**

- [ ] Frontend renders WAJE-owned Answer Package and visualization plan.
- [ ] Frontend does not infer business truth from raw evidence payloads.
- [ ] Question tool supports up to 3-4 short questions, up to 3 options each, recommended inference, and `tell the agent to do differently`.
- [ ] Technical internals stay out of ordinary UI while audit/debug details remain available.

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
