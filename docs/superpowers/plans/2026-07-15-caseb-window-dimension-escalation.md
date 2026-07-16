# Case B window binding and dimension escalation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for every behavior change. This worktree is intentionally dirty; no task may stage or commit files.

**Goal:** Preserve the confirmed target/baseline pair through three-factor decomposition, keep every user-required claim visible through verification, and add a non-blocking data-driven dimension localization layer after the core formula result.

**Architecture:** Window identity and comparison role remain separate values. Required claim obligations are enumerated before evidence readiness is evaluated. Generic paid-amount change explanations receive an auxiliary independent dimension screen over contract-compatible aggregate dimensions; explicit dimension questions continue to use required segment analysis, and joint attribution runs only when two dimensions are explicitly selected and supported.

**Tech Stack:** Python, LangGraph workflow state, ClickHouse analysis contracts, YAML capability contracts, pytest, DeepSeek through `ConversationAgentCore`.

## Global Constraints

- Preserve every existing dirty-tree change and artifact.
- Do not reset, clean, checkout, overwrite, stage, commit, or run the full suite.
- Do not add a date, question string, case id, or provider-output special case.
- Keep permission, SQL safety, snapshot/release, data contract, completeness, reconciliation, provenance, and verifier boundaries hard.
- Formula contribution and dimension localization stay in separate claim groups and separate rankings.
- Auxiliary dimension failure must not block a publishable comparative or formula claim.
- Keep DeepSeek raw output unchanged in real-run artifacts.

---

### Task 1: Canonical target/baseline binding

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase7/test_claim_scoped_window_projection.py`
- Test: `tests/phase7/test_caseb_factor_continuation.py`

**Interfaces:**
- Consumes: confirmed `baseline_binding.candidates`, authoritative rows with `window_id` and `window_role`.
- Produces: claim resolution with `primary_baselines` for required-only and mixed routes; comparison parameters keep `baseline_group=baseline` while the selected window id remains `previous_day` or another contract id.

- [ ] Write RED tests proving required-only routes contain `primary_baselines`, arbitrary baseline window ids project to the `baseline` role, and no auxiliary claim is needed for projection.
- [ ] Run the named tests and confirm the rerun-09 shape fails with zero decompositions.
- [ ] Make `_reconcile_auxiliary_claim_intents` always create canonical claim resolution and keep window ids out of group-role parameters.
- [ ] Run the focused projection and Case B tests until green.

### Task 2: Required-claim obligation verification and business rendering

**Files:**
- Modify: `bi_agent/runtime/answer_package.py`
- Test: `tests/phase4/test_workflow_artifacts_answer.py`
- Test: `tests/phase7/test_caseb_factor_continuation.py`
- Test: `tests/phase7/test_agent_core_bridge.py`

**Interfaces:**
- Consumes: `required_claim_intents`, all evidence for each claim type, accepted claims, resolved-window labels, metric business labels.
- Produces: obligation states `satisfied`, `draft_missing`, `evidence_degraded`, or `evidence_absent`; partial delivery for unresolved required claims; business text that never displays metric ids or window ids.

- [ ] Write RED tests for a comparative claim plus degraded formula evidence, zero-claim typed terminal boundaries, and passed-path label projection.
- [ ] Confirm formula currently disappears from required gaps and passed-path text exposes internal ids.
- [ ] Enumerate required obligations first, then classify evidence/claim state; preserve legal terminal-boundary delivery.
- [ ] Render metric, window, and reviewed dimension labels at authority projection time for both passed and degraded paths.
- [ ] Run focused answer-package and Core bridge tests until green.

### Task 3: Executable independent dimension screen

**Files:**
- Create: `bi_agent/capabilities/candidate_dimension_screen.py`
- Create: `contracts/capabilities/candidate-dimension-screen.yaml`
- Modify: `contracts/runtime/clickhouse-analysis-bindings.yaml`
- Test: `tests/phase7/test_candidate_dimension_screen.py`
- Test: `tests/phase4/test_analysis_contracts.py`

**Interfaces:**
- Consumes: independent per-dimension target/baseline aggregates containing `paid_amount` and `paid_orders`, overall paid-amount delta, candidate dimension ids, minimum visible sample size.
- Produces: per-dimension coverage/reconciliation/concentration profiles, one selected actionable dimension, safe top lifts/drags, selected segment numeric facts, and visible quality limitations.

- [ ] Write RED tests for independent ranking, entrant/exit zero-fill under complete windows, Unknown preservation, sparse suppression, reconciliation, partial-dimension survival, and distinct device brand/model labels.
- [ ] Confirm no executable candidate screen exists and the contract currently asks for a joint scan.
- [ ] Implement the pure capability using target/baseline roles, unioned segments, safe visibility thresholds, and ranking by evidence quality plus leading known-segment movement.
- [ ] Bind the capability to independent `dimension_contribution_scan`; add `device_model` as its own aggregate dimension; keep `device_brand` labelled as device brand.
- [ ] Run capability and analysis-contract tests until green.

### Task 4: Conditional dimension escalation after core factors

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/revenue_runtime_plan.py`
- Modify: `bi_agent/runtime/llm_prompts.py` only for business-facing auxiliary-claim guidance.
- Test: `tests/phase7/test_candidate_claim_routing.py`
- Test: `tests/phase4/test_revenue_runtime_plan.py`
- Test: `tests/phase7/test_caseb_factor_continuation.py`

**Interfaces:**
- Consumes: paid-amount change question family, required formula claim, runtime-compatible dimensions, remaining exploration budget.
- Produces: auxiliary `segment_contribution_or_mix_shift` routed through `candidate_dimension_screen`; explicit dimensions remain required segment claims; formula and dimension evidence stay independently publishable.

- [ ] Write RED tests showing generic factor questions auto-add only the auxiliary independent screen, explicit single dimensions add segment analysis without joint analysis, and auxiliary gaps cannot block formula output.
- [ ] Confirm current static `factor_topk` mixes formula, segment, and joint routes.
- [ ] Derive candidate dimensions from target-metric/source compatibility and attach the screen after obligation resolution without rewriting user material.
- [ ] Execute the screen before synthesis, expose its evidence as auxiliary, and prohibit joint promotion without two selected dimensions plus incremental value.
- [ ] Run focused routing, runtime-plan, and Case B tests until green.

### Task 5: Verification and real rerun

**Files:**
- Preserve: `artifacts/phase7/human-led-q1/case-b-rerun-09/`
- Create through runtime only: `artifacts/phase7/human-led-q1/case-b-rerun-10/`

- [ ] Run only the affected test files plus syntax compilation and `git diff --check`.
- [ ] Start a fresh Case B through `ConversationAgentCore`; answer only a displayed material clarification.
- [ ] Verify direction, three-factor reconciliation, first-charge diagnostic status, payment-success optional boundary, dimension-screen selection, separate claim groups, provenance, verifier result, final audit, and raw DeepSeek output.
- [ ] If a new real-run failure appears, freeze the new artifact and return to root-cause analysis before changing more code.
