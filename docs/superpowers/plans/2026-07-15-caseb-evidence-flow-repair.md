# Paid-amount evidence flow repair implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a complete target-versus-baseline comparison and reconciled paid-amount driver decomposition reach a verified customer answer while keeping stability patterns and unavailable optional factors as independent auxiliary branches.

**Architecture:** Direct target/baseline change facts use the existing signed window comparison authority. Repeated-pattern capabilities keep their statistical thresholds, and the evidence reducer selects publishable evidence per required claim instead of flattening every capability into one global status. Runtime liveness protects supported required claims from an LLM degradation suggestion; verifier completeness rejects an unexplained empty claim set when those claims have publishable authority.

**Tech Stack:** Python, LangGraph workflow state, YAML runtime contracts, pytest, DeepSeek through `ConversationAgentCore`.

## Global Constraints

- Preserve the dirty worktree and all unrelated edits.
- Do not reset, clean, checkout, overwrite, stage, commit, or run the full suite.
- Do not add special cases for a date, question string, case id, or provider output.
- Keep permission, SQL safety, snapshot/release, query completeness, source authority, reconciliation, provenance, and verifier checks hard.
- Keep DeepSeek raw responses unchanged in the real-run artifact.
- Use `ConversationAgentCore` or the Gateway for the real rerun.

---

### Task 1: Direct comparative-change authority

**Files:**
- Modify: `bi_agent/runtime/capability_harness.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase4/test_capability_harness.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Consumes: a validated `BoundCapabilityInput` whose authoritative query contract contains one target window and at least one baseline window.
- Produces: a `CapabilityEvidenceEnvelope` with `target_value`, `baseline_value`, `absolute_change`, and `relative_change`; it has no repeated-pattern limitations.

- [ ] **Step 1: Write the failing direct-comparison tests**

```python
def test_compare_periods_uses_bound_window_values_without_pattern_thresholds():
    envelope = execute_capability(authoritative_compare_request)
    assert envelope.typed_payload["relative_change"] == Decimal("0.013472886060069909")
    assert "weak_direction" not in envelope.limitations
    assert "below_materiality_floor" not in envelope.limitations

def test_compare_periods_preserves_negative_direction_symmetrically():
    envelope = execute_capability(authoritative_decrease_request)
    assert envelope.typed_payload["relative_change"] < 0
    assert envelope.wording_limit == "quantified"
```

- [ ] **Step 2: Run the tests and observe the current pattern-scan failure**

Run: `pytest -q tests/phase4/test_capability_harness.py -k 'compare_periods_uses_bound_window or compare_periods_preserves_negative'`

Expected: FAIL because `compare_periods` currently returns pattern fields such as `comparable_periods` instead of exact window change facts.

- [ ] **Step 3: Route direct comparison through signed window aggregation**

```python
PATTERN_COMPARE_CAPABILITIES = frozenset({
    "compare_period_phases",
    "rolling_window_compare",
    "weekday_calendar_compare",
    "event_window_compare",
})
WINDOW_METRIC_COMPARE_CAPABILITIES = frozenset({
    "compare_periods",
    "market_health_compare",
})
```

- [ ] **Step 4: Run the focused tests and adjacent window-authority tests**

Run: `pytest -q tests/phase4/test_capability_harness.py tests/phase4/test_market_window_evidence.py -k 'compare_periods or window_compare or window_metric'`

Expected: PASS.

---

### Task 2: Claim-scoped evidence reduction and liveness

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/llm_prompts.py`
- Test: `tests/phase4/test_llm_workflow.py`
- Test: `tests/phase7/test_caseb_factor_continuation.py`

**Interfaces:**
- Consumes: required and candidate claim partitions plus capability evidence envelopes.
- Produces: `evidence_brief.claim_evidence`, global limitations derived only from unresolved required claims, and diagnostic auxiliary limitations that cannot block supported siblings.

- [ ] **Step 1: Write failing evidence-survival tests**

```python
def test_reduce_evidence_keeps_reconciled_driver_when_auxiliary_pattern_is_weak():
    state = caseb_state_with_weak_pattern_and_reconciled_driver()
    _reduce_evidence(state)
    assert state["evidence_brief"]["primary_capability"] == "driver_decomposition"
    assert state["evidence_brief"]["claim_evidence"]["formula_component_contribution"]["status"] == "publishable"
    assert "missing_formula_component:payment_success_chain" not in state["evidence_brief"]["limitations"]

def test_provider_degrade_cannot_drop_publishable_required_claims():
    state = caseb_state_with_publishable_required_claims(next_action="degrade")
    assert _route_after_next_action(state) == "synthesize"
```

- [ ] **Step 2: Run the RED tests**

Run: `pytest -q tests/phase4/test_llm_workflow.py tests/phase7/test_caseb_factor_continuation.py -k 'claim_scoped or publishable_required or reconciled_driver'`

Expected: FAIL because `_reduce_evidence` currently chooses any pattern first and unions every limitation.

- [ ] **Step 3: Implement claim-scoped selection**

```python
def _publishable_evidence_by_claim(state):
    # Select the strongest claim-ready envelope for each requested claim.
    # Keep candidate and unselected branch gaps in diagnostics only.
    ...
```

Update `next_action` guidance so materiality and repeated-pattern limitations constrain only their claim branch; supported core claims must still be synthesized.

- [ ] **Step 4: Run focused workflow tests**

Run: `pytest -q tests/phase4/test_llm_workflow.py tests/phase7/test_caseb_factor_continuation.py -k 'next_action or reduce_evidence or factor_continuation'`

Expected: PASS.

---

### Task 3: Formula-candidate locality and auxiliary stability context

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/llm_prompts.py`
- Test: `tests/phase7/test_candidate_claim_routing.py`
- Test: `tests/phase7/test_core_driver_capability_binding.py`

**Interfaces:**
- Consumes: selected executable formula path, reconciled driver evidence, and optional LLM candidate claim `baseline_stability`.
- Produces: selected formula publication state, branch-local formula gaps, and an optional `rolling_window_compare` route that preserves `previous_day` as the primary baseline.

- [ ] **Step 1: Write failing branch-locality tests**

```python
def test_unselected_formula_gaps_do_not_limit_reconciled_core_path():
    brief = reduce_formula_and_driver_evidence(reconciled=True, extra_blocked_candidates=20)
    assert brief["claim_evidence"]["formula_component_contribution"]["status"] == "publishable"
    assert brief["limitations"] == []

def test_change_explanation_may_add_stability_as_nonblocking_candidate():
    intent = normalize_business_intent(provider_change_intent_with_stability_candidate)
    assert intent["claim_intent_roles"]["baseline_stability"] == "llm_candidate"
```

- [ ] **Step 2: Run the RED tests**

Run: `pytest -q tests/phase7/test_candidate_claim_routing.py tests/phase7/test_core_driver_capability_binding.py -k 'formula_gaps or stability_as_nonblocking'`

Expected: FAIL until the selected-path and candidate guidance are explicit.

- [ ] **Step 3: Keep candidate gaps local and prompt for safe auxiliary context**

The provider may propose recent-baseline stability as `llm_candidate`; the existing local route compiler validates it and adds `rolling_7_day_baseline` only when queryable. It never changes the primary `previous_day` baseline and never becomes a required conclusion without an explicit user ask.

- [ ] **Step 4: Run the candidate-routing and driver-binding tests**

Run: `pytest -q tests/phase7/test_candidate_claim_routing.py tests/phase7/test_core_driver_capability_binding.py`

Expected: PASS.

---

### Task 4: Required-claim completeness at verification

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/answer_package.py` only if the workflow-level check cannot preserve the existing authority boundary.
- Test: `tests/phase4/test_workflow_artifacts_answer.py`
- Test: `tests/phase4/test_final_answer_audit.py`

**Interfaces:**
- Consumes: required claim intents, publishable evidence-by-claim, draft claims, verifier result, and the exact delivery text.
- Produces: a repairable verification failure when a publishable required claim is silently absent; legitimate blocked/degraded terminal boundaries remain valid.

- [ ] **Step 1: Write the failing zero-claim completeness test**

```python
def test_publishable_required_evidence_cannot_finish_with_zero_claims():
    result = verify_required_claim_delivery(state_with_reconciled_driver_and_no_claims)
    assert result["status"] != "passed"
    assert "missing_required_claim" in result["errors"]
```

- [ ] **Step 2: Run the RED test**

Run: `pytest -q tests/phase4/test_workflow_artifacts_answer.py tests/phase4/test_final_answer_audit.py -k 'publishable_required or zero_claim'`

Expected: FAIL because an empty claim set currently passes.

- [ ] **Step 3: Add the narrow completeness check**

Only claim intents with claim-ready, authority-bound, publishable evidence are required. Optional candidates, unavailable factors, and legitimate contract/window terminal boundaries do not create missing-claim errors.

- [ ] **Step 4: Run focused verifier tests**

Run: `pytest -q tests/phase4/test_workflow_artifacts_answer.py tests/phase4/test_final_answer_audit.py tests/phase7/test_core_driver_capability_binding.py`

Expected: PASS.

---

### Task 5: Verification and real Case B rerun

**Files:**
- Preserve: `artifacts/phase7/human-led-q1/case-b-rerun-05/`
- Create through runtime only: `artifacts/phase7/human-led-q1/case-b-rerun-06/`

- [ ] **Step 1: Run the affected regression slice**

Run: `pytest -q tests/phase4/test_capability_harness.py tests/phase4/test_llm_workflow.py tests/phase4/test_workflow_artifacts_answer.py tests/phase4/test_final_answer_audit.py tests/phase7/test_candidate_claim_routing.py tests/phase7/test_caseb_factor_continuation.py tests/phase7/test_core_driver_capability_binding.py`

Expected: PASS. Do not run the full suite.

- [ ] **Step 2: Inspect the diff and dirty-tree preservation**

Run: `git diff --check` and `git status --short --branch`.

Expected: no whitespace errors; all unrelated user changes remain present.

- [ ] **Step 3: Run Case B through `ConversationAgentCore`**

Run the original question in a fresh thread and artifact directory. If the baseline question opens, answer with the exact displayed previous-day recommended option. Do not auto-answer any new material clarification.

- [ ] **Step 4: Review the artifact**

Confirm target/baseline values, direct direction, driver contributions, optional payment-success boundary, auxiliary stability status, structured claims, evidence refs, verifier result, exact final answer audit, and raw DeepSeek responses.
