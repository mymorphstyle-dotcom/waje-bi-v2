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

## Phases 0-4: Historical Baseline

Status: superseded by the 2026-07-07 Post-Phase 4 Rebaseline below.

The original Phase 0-4 checklist described the first implementation program:
contract setup, graph/compiler contracts, semantic-query handoff, and the first
generalized pattern slice. Its checkbox criteria were useful during the initial
build, but they are no longer the current launch acceptance source.

Current evidence for this historical segment lives in:

- [docs/phase-1-contract-foundation-breakdown.md](/Users/luka/work/waje-bi-v2/docs/phase-1-contract-foundation-breakdown.md:1)
- [docs/phase-3-completion-status.md](/Users/luka/work/waje-bi-v2/docs/phase-3-completion-status.md:1)
- [docs/phase-4-closeout-status.md](/Users/luka/work/waje-bi-v2/docs/phase-4-closeout-status.md:1)
- [docs/reviews/phase4-ten-case-node-audit-20260707.md](/Users/luka/work/waje-bi-v2/docs/reviews/phase4-ten-case-node-audit-20260707.md:1)

Current launch acceptance starts at the Post-Phase 4 Rebaseline and is proven by
Phase 5 through Phase 8 closeout evidence. Do not use old Phase 0-4 checkbox
items as open launch gates.

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

Current progress (2026-07-08): Phase 7 runtime foundation now has conversation contracts, Postgres conversation schema, Python Postgres store with audit writes, production-safe TypeScript gateway store selection, thread/topic/turn/run context assembly, result reuse decisions, memory proposals, Python Agent Core bridge, and the public gateway routes for threads, messages, run events, clarifications, artifact continue, artifact read/export, and memory proposal accept/reject. Conversation routing now has an LLM-backed orchestrator for turn intent and topic relation, with local enum validation, pending-clarification checks, running-run queue normalization, and hard guards for off-topic and unsupported requests. Context manifests now record result refs as first-class context sources, mark whether each source can support claims, and carry permission scope, source version, expiry, and claim-use metadata; memory items carry TTL, refresh rule, confidence, and revocation path. Answer Package now includes a validated `visualization_plan` derived from verified claim groups, and the workbench renders those visual blocks through the existing TraceRun UI contract. Clarification answers now bind back to the pending topic, clear pending state, record the clarification outcome, and resume the same run through Agent Core. Runtime question tool now blocks ambiguous runs with a structured clarification payload, recommended inference, `tell the agent to do differently`, pending clarification state, and run-level audit events; no-answer runs waiting for clarification now appear in the agent workbench as business-readable process traces. Run event streams now include a business-facing `process` summary while retaining raw payloads for audit/debug; persisted run nodes are also emitted as business process events for intent, accepted plan, capability progress, repair/degrade/block, and verifier stages, and completed Postgres Answer Package runs now inject recorded run nodes into the workbench timeline. The ordinary workbench hides raw audit JSON by default, with debug audit available only through an explicit `processSummary.debugAudit` gate. Artifact access now persists refs in Postgres, validates role visibility before open/continue/export, records allow/block/open/export audit events, and passes claim support only when result refs, snapshot, contract, and permission checks match. Coverage is tracked in `evals/phase7/conversation_scenarios.yaml` with 60 natural-language multi-turn scenarios.

**Deliverables:**

- [x] TypeScript gateway routes for thread creation, message submission, run event stream handles, clarifications, artifact continue, and memory proposal decisions.
- [x] Conversation runtime contracts for `Thread`, `Topic`, `Turn`, `Run`, `ContextManifest`, `ReuseDecision`, `MemoryItem`, and `MemoryProposal`.
- [x] LLM-backed Conversation Orchestrator for turn intent, topic relation, clarification answer, challenge, artifact continue, capability question, off-topic, unsupported, memory update, and mixed-question routing, with local validation and audit.
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

Current progress (2026-07-08): Phase 8 production gates are in place. `GET /api/runs/:runId/audit-trace` returns the run, latest Answer Package, claim groups, verifier output, run nodes, evidence refs, result/query refs, contract versions, snapshot ids, and audit events from the Postgres runtime store. `GET /api/runs/:runId/rerun-comparability?candidateRunId=...` compares snapshot ids, contract versions, and query/result refs between two runs so reruns can state whether they are comparable to the original run. `GET /api/runs/launch-dashboard` lists slow, failed, degraded, blocked, verifier-failed, capability-error, compiler-blocked, permission-spike, contract-mismatch, and ledger-mismatch runs for launch review. Artifact open, export, and continue-investigation paths now enforce role visibility, audit allowed and blocked access, and filter summary claims, claim groups, and visualization blocks with the same permission rules as artifact sections. `GET /api/health` checks the gateway route, Python BI Agent Core import, Postgres runtime store, ClickHouse access, and LangGraph adapter readiness. Capability execution now turns call budget exhaustion, row budget excess, result-ref budget excess, and timeout markers into blocked evidence envelopes that flow into Answer Package visible limitations. `bi_agent/runtime/release_manifest.json` records rollback refs, owners, paths, and required checks for contracts, ledgers, capability cards, prompt/recipe, and verifier policy. The release-candidate launch eval passed for all 8 expectation packages; see `docs/reviews/phase8-release-candidate-eval-20260708.md`.

**Deliverables:**

- [x] Permission-blocked claim behavior and permission-filtered artifacts.
- [x] Audit trace from answer to run, contract version, evidence, claim, verifier result, and query refs.
- [x] Snapshot and rerun comparability rules.
- [x] Capability timeout, row/result budget, and degradation behavior.
- [x] Health checks for frontend/gateway, Python BI Agent Core, Postgres runtime mirror, ClickHouse access, and LangGraph adapter.
- [x] Version rollback for contract, ledger, capability card, prompt/recipe, and verifier policy.
- [x] Launch dashboard for slow run, capability error, compiler block, verifier failure, permission spike, contract mismatch, and ledger mismatch.

**Acceptance:**

- [x] Slow, failed, degraded, blocked, verifier-failed, capability-error, compiler-blocked, permission-spike, contract-mismatch, and ledger-mismatch runs are locatable.
- [x] Budget skips enter Answer Package limitations or follow-up.
- [x] Reruns state whether they are comparable to the original run.
- [x] Release candidate runs full acceptance eval.

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
