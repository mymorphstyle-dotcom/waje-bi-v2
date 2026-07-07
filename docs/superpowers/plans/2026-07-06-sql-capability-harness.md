# SQL Capability Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the SQL Capability Harness as the WAJE-owned tool layer that lets the LLM compose business capability calls without seeing or executing raw SQL.

**Architecture:** Add a small typed harness in Python around the existing Phase 4 capabilities. Capability cards stay business-readable and LLM-visible; local request validation, SQL/result refs, evidence envelopes, and budget accounting stay inside WAJE runtime code. Keep the first implementation static and in-memory, then wire persistence later when the runtime ledger lands.

**Tech Stack:** Python 3 stdlib dataclasses/unittest, existing `bi_agent` runtime, existing Phase 4 capability functions, existing ClickHouse runtime and SQL safety validator.

---

## File Structure

- Create `bi_agent/runtime/capability_models.py`: shared dataclasses for capability cards, requests, evidence envelopes, and budget state.
- Create `bi_agent/runtime/capability_registry.py`: static public capability catalog and helpers for LLM-visible card summaries.
- Create `bi_agent/runtime/exploration_budget.py`: R&D budget defaults and call accounting.
- Create `bi_agent/runtime/capability_harness.py`: dispatch accepted capability requests to existing local capability functions and return normalized evidence envelopes.
- Modify `bi_agent/runtime/langgraph_workflow.py`: pass capability cards and budget state into route planning, execute accepted graph through the harness, and keep evidence envelopes normalized.
- Modify `bi_agent/runtime/compiler.py`: validate requested capability ids against the registry instead of a duplicated constant set.
- Modify `bi_agent/runtime/llm_prompts.py`: tell the LLM to plan against capability cards and preserve budget quality priority during R&D.
- Add `tests/phase4/test_capability_registry.py`: registry/card contract checks.
- Add `tests/phase4/test_exploration_budget.py`: budget accounting and hard-limit behavior.
- Add `tests/phase4/test_capability_harness.py`: normalized evidence envelope checks for representative capability calls.
- Update `docs/phase-4-sql-capability-harness.md` if implementation changes any public field names.

### Task 1: Capability Models

**Files:**
- Create: `bi_agent/runtime/capability_models.py`
- Test: `tests/phase4/test_capability_registry.py`

- [ ] **Step 1: Add failing model contract tests**

```python
import unittest

from bi_agent.runtime.capability_models import (
    BudgetState,
    CapabilityCard,
    CapabilityEvidenceEnvelope,
    CapabilityRequest,
)


class CapabilityModelTest(unittest.TestCase):
    def test_capability_card_llm_summary_hides_physical_sql(self):
        card = CapabilityCard(
            capability_id="compare_periods",
            business_name="周期对比",
            description="Compare one metric between target and baseline periods.",
            input_schema={"metric": "metric_id"},
            output_schema={"evidence_ref": "string"},
            supported_question_families=("custom_baseline_comparison",),
            supported_grains=("day",),
            allowed_claim_types=("comparative_change",),
            default_evidence_type="statistical_association",
            cost_tier="low",
            runtime_tier="short",
            preconditions=("metric_contract_active",),
            failure_modes=("coverage_gap",),
        )

        summary = card.to_llm_summary()

        self.assertEqual(summary["capability_id"], "compare_periods")
        self.assertNotIn("sql", repr(summary).lower())
        self.assertNotIn("table", repr(summary).lower())

    def test_request_and_envelope_keep_business_labels(self):
        budget = BudgetState(mode="research", used_capability_calls=0, soft_limit=50, hard_limit=100)
        request = CapabilityRequest(
            run_id="run-1",
            accepted_graph_id="graph-1",
            graph_version=1,
            capability_id="compare_periods",
            question_family="custom_baseline_comparison",
            target_claim="comparative_change",
            claim_type="comparative_change",
            metric="paid_amount_ngn",
            scope="all_successful_paid_orders",
            time_window="2026-01-01..2026-06-30",
            baseline={"label": "Q1", "start": "2026-01-01", "end": "2026-04-01"},
            target={"label": "Q2", "start": "2026-04-01", "end": "2026-07-01"},
            grain="day",
            filters={},
            dimensions=(),
            contract_versions={"metric": "paid_amount.v1"},
            role="analyst",
            budget_state=budget,
            llm_business_reason="Compare Q2 against Q1.",
            params={},
        )
        envelope = CapabilityEvidenceEnvelope(
            evidence_ref="compare_periods:run-1:0",
            capability_id=request.capability_id,
            question_family=request.question_family,
            target_claim=request.target_claim,
            claim_type=request.claim_type,
            metric=request.metric,
            scope=request.scope,
            grain=request.grain,
            baseline_label="Q1",
            target_label="Q2",
            time_window=request.time_window,
            numeric_facts={"percent_delta": 0.15},
            typed_payload={"comparison_type": "period_average"},
            result_refs=("sqlhash-1",),
            sql_hashes=("sqlhash-1",),
            evidence_type="statistical_association",
            strength="medium",
            wording_limit="supported",
            limitations=(),
            disabled_degraded_blocked_path_refs=(),
            verifier_handoff={"requires_baseline_label": "Q1", "requires_target_label": "Q2"},
            admin_audit_ref="audit-1",
        )

        self.assertEqual(envelope.baseline_label, "Q1")
        self.assertEqual(envelope.target_label, "Q2")
        self.assertEqual(envelope.numeric_facts["percent_delta"], 0.15)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
python3 -m unittest tests.phase4.test_capability_registry
```

Expected: fail with `ModuleNotFoundError` for `bi_agent.runtime.capability_models`.

- [ ] **Step 3: Add the minimal dataclasses**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class BudgetState:
    mode: str
    used_capability_calls: int
    soft_limit: int
    hard_limit: int

    def to_llm_summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "used_capability_calls": self.used_capability_calls,
            "soft_limit": self.soft_limit,
            "hard_limit": self.hard_limit,
            "budget_instruction": "do_not_trade_answer_quality_for_cost_during_research",
        }


@dataclass(frozen=True)
class CapabilityCard:
    capability_id: str
    business_name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    supported_question_families: tuple[str, ...]
    supported_grains: tuple[str, ...]
    allowed_claim_types: tuple[str, ...]
    default_evidence_type: str
    cost_tier: str
    runtime_tier: str
    preconditions: tuple[str, ...]
    failure_modes: tuple[str, ...]

    def to_llm_summary(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "business_name": self.business_name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "supported_question_families": list(self.supported_question_families),
            "supported_grains": list(self.supported_grains),
            "allowed_claim_types": list(self.allowed_claim_types),
            "default_evidence_type": self.default_evidence_type,
            "cost_tier": self.cost_tier,
            "runtime_tier": self.runtime_tier,
            "preconditions": list(self.preconditions),
            "failure_modes": list(self.failure_modes),
        }


@dataclass(frozen=True)
class CapabilityRequest:
    run_id: str
    accepted_graph_id: str
    graph_version: int
    capability_id: str
    question_family: str
    target_claim: str
    claim_type: str
    metric: str
    scope: str
    time_window: str
    baseline: Mapping[str, Any]
    target: Mapping[str, Any]
    grain: str
    filters: Mapping[str, Any]
    dimensions: tuple[str, ...]
    contract_versions: Mapping[str, str]
    role: str
    budget_state: BudgetState
    llm_business_reason: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class CapabilityEvidenceEnvelope:
    evidence_ref: str
    capability_id: str
    question_family: str
    target_claim: str
    claim_type: str
    metric: str
    scope: str
    grain: str
    baseline_label: str
    target_label: str
    time_window: str
    numeric_facts: Mapping[str, Any]
    typed_payload: Mapping[str, Any]
    result_refs: tuple[str, ...]
    sql_hashes: tuple[str, ...]
    evidence_type: str
    strength: str
    wording_limit: str
    limitations: tuple[str, ...]
    disabled_degraded_blocked_path_refs: tuple[str, ...]
    verifier_handoff: Mapping[str, Any]
    admin_audit_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.__dict__.items()
        }
```

- [ ] **Step 4: Run model tests**

Run:

```bash
python3 -m unittest tests.phase4.test_capability_registry
```

Expected: pass.

### Task 2: Static Capability Registry

**Files:**
- Create: `bi_agent/runtime/capability_registry.py`
- Modify: `tests/phase4/test_capability_registry.py`

- [ ] **Step 1: Extend registry tests**

Add this to `tests/phase4/test_capability_registry.py`:

```python
from bi_agent.runtime.capability_registry import (
    get_capability_card,
    llm_capability_cards,
    public_capability_ids,
)


class CapabilityRegistryTest(unittest.TestCase):
    def test_registry_contains_general_catalog(self):
        expected = {
            "metric_coverage_profile",
            "metric_timeseries",
            "data_quality_profile",
            "compare_periods",
            "compare_period_phases",
            "rolling_window_compare",
            "weekday_calendar_compare",
            "event_window_compare",
            "formula_decompose",
            "component_contribution",
            "segment_breakdown",
            "segment_shift_compare",
            "candidate_dimension_screen",
            "joint_attribution",
            "outlier_scan",
            "change_point_scan",
            "evidence_reduce",
            "answer_verify",
        }

        self.assertEqual(set(public_capability_ids()), expected)

    def test_llm_cards_do_not_expose_physical_details(self):
        cards = llm_capability_cards()
        text = repr(cards).lower()

        self.assertIn("compare_periods", text)
        self.assertNotIn("paid_order_success_clean", text)
        self.assertNotIn("select ", text)
        self.assertNotIn("clickhouse", text)

    def test_unknown_capability_raises_key_error(self):
        with self.assertRaises(KeyError):
            get_capability_card("raw_sql")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.phase4.test_capability_registry
```

Expected: fail with `ModuleNotFoundError` for `bi_agent.runtime.capability_registry`.

- [ ] **Step 3: Add static registry**

Create `bi_agent/runtime/capability_registry.py`:

```python
from __future__ import annotations

from bi_agent.runtime.capability_models import CapabilityCard


_CATALOG: dict[str, CapabilityCard] = {
    capability_id: CapabilityCard(
        capability_id=capability_id,
        business_name=business_name,
        description=description,
        input_schema={"metric": "metric_id", "scope": "business_scope", "time_window": "time_window"},
        output_schema={"evidence_ref": "string", "typed_payload": "object", "limitations": "list"},
        supported_question_families=question_families,
        supported_grains=("day", "week", "month"),
        allowed_claim_types=claim_types,
        default_evidence_type=evidence_type,
        cost_tier=cost_tier,
        runtime_tier=runtime_tier,
        preconditions=("metric_contract_active", "aggregate_permission_allowed"),
        failure_modes=("coverage_gap", "unsupported_grain", "missing_contract"),
    )
    for capability_id, business_name, description, question_families, claim_types, evidence_type, cost_tier, runtime_tier in (
        ("metric_coverage_profile", "指标覆盖检查", "Check metric coverage for the requested scope and grain.", ("data_quality_or_evidence_review",), ("contract_coverage_and_trust_boundary",), "insufficient", "low", "short"),
        ("metric_timeseries", "指标时间序列", "Build an aggregate time series for a metric.", ("pattern_explanation", "paid_amount_change_explanation"), ("comparative_change", "recurring_pattern_existence"), "statistical_association", "low", "short"),
        ("data_quality_profile", "数据质量检查", "Review data trust boundaries and limitations.", ("data_quality_or_evidence_review", "pattern_explanation"), ("contract_coverage_and_trust_boundary",), "insufficient", "low", "short"),
        ("compare_periods", "周期对比", "Compare target and baseline periods for a metric.", ("custom_baseline_comparison", "paid_amount_change_explanation"), ("comparative_change", "baseline_stability"), "statistical_association", "low", "short"),
        ("compare_period_phases", "周期内阶段对比", "Compare phases inside a period.", ("pattern_explanation",), ("recurring_pattern_existence",), "statistical_association", "medium", "normal"),
        ("rolling_window_compare", "滚动窗口对比", "Compare rolling windows and sustained movement.", ("pattern_explanation", "paid_amount_change_explanation"), ("baseline_stability", "comparative_change"), "statistical_association", "medium", "normal"),
        ("weekday_calendar_compare", "星期日历对比", "Compare weekday or calendar buckets.", ("pattern_explanation",), ("recurring_pattern_existence",), "statistical_association", "medium", "normal"),
        ("event_window_compare", "事件窗口对比", "Compare metric movement around a known event or assumption.", ("business_object_impact_review", "pattern_explanation"), ("business_object_candidate_impact", "candidate_mechanism"), "candidate_mechanism", "medium", "normal"),
        ("formula_decompose", "公式拆解", "Decompose a metric into available formula components.", ("paid_amount_change_explanation", "revenue_health_review"), ("formula_component_contribution",), "accounting_contribution", "medium", "normal"),
        ("component_contribution", "组件贡献", "Quantify component contribution between periods.", ("paid_amount_change_explanation", "revenue_health_review"), ("formula_component_contribution",), "accounting_contribution", "medium", "normal"),
        ("segment_breakdown", "分群拆解", "Break down metric values across one dimension.", ("segment_or_factor_attribution", "paid_amount_change_explanation"), ("segment_contribution_or_mix_shift",), "statistical_association", "medium", "normal"),
        ("segment_shift_compare", "分群结构变化", "Compare segment mix between baseline and target.", ("segment_or_factor_attribution", "paid_amount_change_explanation"), ("segment_contribution_or_mix_shift",), "statistical_association", "medium", "normal"),
        ("candidate_dimension_screen", "候选维度筛选", "Rank eligible dimensions before deeper attribution.", ("segment_or_factor_attribution", "pattern_explanation"), ("segment_contribution_or_mix_shift",), "statistical_association", "high", "long"),
        ("joint_attribution", "组合归因", "Test selected dimension combinations.", ("segment_or_factor_attribution", "paid_amount_change_explanation"), ("geo_device_combination_attribution", "segment_contribution_or_mix_shift"), "statistical_association", "high", "long"),
        ("outlier_scan", "异常周期检查", "Find anomalous periods or segments that affect a claim.", ("anomaly_or_black_swan_review", "pattern_explanation"), ("external_shock_candidate_or_anomaly", "payment_chain_risk_or_anomaly"), "statistical_association", "medium", "normal"),
        ("change_point_scan", "变化点检查", "Detect candidate breakpoints in a metric series.", ("anomaly_or_black_swan_review", "paid_amount_change_explanation"), ("external_shock_candidate_or_anomaly", "comparative_change"), "statistical_association", "medium", "normal"),
        ("evidence_reduce", "证据整理", "Merge compatible evidence into a claim-ready summary.", ("pattern_explanation", "paid_amount_change_explanation", "segment_or_factor_attribution"), ("comparative_change", "recurring_pattern_existence"), "insufficient", "low", "short"),
        ("answer_verify", "答案校验", "Verify final claims against evidence and contracts.", ("data_quality_or_evidence_review", "pattern_explanation", "paid_amount_change_explanation", "segment_or_factor_attribution"), ("contract_coverage_and_trust_boundary", "comparative_change"), "insufficient", "low", "short"),
    )
}


def public_capability_ids() -> tuple[str, ...]:
    return tuple(_CATALOG)


def get_capability_card(capability_id: str) -> CapabilityCard:
    return _CATALOG[capability_id]


def llm_capability_cards() -> list[dict]:
    return [card.to_llm_summary() for card in _CATALOG.values()]
```

- [ ] **Step 4: Run registry tests**

Run:

```bash
python3 -m unittest tests.phase4.test_capability_registry
```

Expected: pass.

### Task 3: Exploration Budget

**Files:**
- Create: `bi_agent/runtime/exploration_budget.py`
- Test: `tests/phase4/test_exploration_budget.py`

- [ ] **Step 1: Add failing budget tests**

```python
import unittest

from bi_agent.runtime.exploration_budget import (
    default_budget,
    record_capability_call,
    should_ask_before_more_exploration,
)


class ExplorationBudgetTest(unittest.TestCase):
    def test_ordinary_research_budget_defaults_to_50_soft_100_hard(self):
        budget = default_budget("ordinary")

        self.assertEqual(budget.mode, "research")
        self.assertEqual(budget.soft_limit, 50)
        self.assertEqual(budget.hard_limit, 100)

    def test_deep_attribution_budget_defaults_to_100(self):
        budget = default_budget("deep_attribution")

        self.assertEqual(budget.soft_limit, 100)
        self.assertEqual(budget.hard_limit, 100)

    def test_hard_limit_requires_user_question(self):
        budget = default_budget("ordinary")
        for _ in range(100):
            budget = record_capability_call(budget)

        self.assertTrue(should_ask_before_more_exploration(budget))

    def test_soft_limit_does_not_block_research(self):
        budget = default_budget("ordinary")
        for _ in range(50):
            budget = record_capability_call(budget)

        self.assertFalse(should_ask_before_more_exploration(budget))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.phase4.test_exploration_budget
```

Expected: fail with `ModuleNotFoundError` for `bi_agent.runtime.exploration_budget`.

- [ ] **Step 3: Add budget implementation**

```python
from __future__ import annotations

from dataclasses import replace

from bi_agent.runtime.capability_models import BudgetState


def default_budget(depth: str) -> BudgetState:
    if depth == "deep_attribution":
        return BudgetState(mode="research", used_capability_calls=0, soft_limit=100, hard_limit=100)
    return BudgetState(mode="research", used_capability_calls=0, soft_limit=50, hard_limit=100)


def record_capability_call(budget: BudgetState) -> BudgetState:
    return replace(budget, used_capability_calls=budget.used_capability_calls + 1)


def should_ask_before_more_exploration(budget: BudgetState) -> bool:
    return budget.used_capability_calls >= budget.hard_limit
```

- [ ] **Step 4: Run budget tests**

Run:

```bash
python3 -m unittest tests.phase4.test_exploration_budget
```

Expected: pass.

### Task 4: Harness Dispatch

**Files:**
- Create: `bi_agent/runtime/capability_harness.py`
- Test: `tests/phase4/test_capability_harness.py`

- [ ] **Step 1: Add failing harness tests**

```python
import unittest

from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest


class CapabilityHarnessTest(unittest.TestCase):
    def test_pattern_scan_returns_normalized_envelope(self):
        request = CapabilityRequest(
            run_id="run-1",
            accepted_graph_id="graph-1",
            graph_version=1,
            capability_id="compare_period_phases",
            question_family="pattern_explanation",
            target_claim="recurring_pattern_existence",
            claim_type="recurring_pattern_existence",
            metric="paid_amount_ngn",
            scope="all_successful_paid_orders",
            time_window="2026-01..2026-03",
            baseline={"label": "middle_or_end"},
            target={"label": "start"},
            grain="month",
            filters={},
            dimensions=(),
            contract_versions={"metric": "paid_amount.v1"},
            role="analyst",
            budget_state=BudgetState(mode="research", used_capability_calls=0, soft_limit=50, hard_limit=100),
            llm_business_reason="Check whether month start is higher than sibling phases.",
            params={
                "rows": [
                    {"month": "2026-01", "phase": "start", "amount": 130},
                    {"month": "2026-01", "phase": "middle", "amount": 100},
                    {"month": "2026-01", "phase": "end", "amount": 90},
                    {"month": "2026-02", "phase": "start", "amount": 140},
                    {"month": "2026-02", "phase": "middle", "amount": 100},
                    {"month": "2026-02", "phase": "end", "amount": 90},
                ],
                "result_refs": ("sqlhash-1",),
                "pattern_family": "intra_period",
                "target_phase": "start",
                "min_periods": 2,
            },
        )

        envelope = execute_capability(request)

        self.assertEqual(envelope.capability_id, "compare_period_phases")
        self.assertEqual(envelope.target_label, "start")
        self.assertEqual(envelope.baseline_label, "middle_or_end")
        self.assertEqual(envelope.result_refs, ("sqlhash-1",))
        self.assertIn("median_uplift", envelope.numeric_facts)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.phase4.test_capability_harness
```

Expected: fail with `ModuleNotFoundError` for `bi_agent.runtime.capability_harness`.

- [ ] **Step 3: Add dispatch implementation for existing pattern capability**

```python
from __future__ import annotations

from typing import Any

from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.runtime.capability_models import CapabilityEvidenceEnvelope, CapabilityRequest


def execute_capability(request: CapabilityRequest) -> CapabilityEvidenceEnvelope:
    if request.capability_id == "compare_period_phases":
        return _execute_compare_period_phases(request)
    raise KeyError(f"unsupported capability_id: {request.capability_id}")


def _execute_compare_period_phases(request: CapabilityRequest) -> CapabilityEvidenceEnvelope:
    params: dict[str, Any] = dict(request.params)
    rows = params.pop("rows")
    result_refs = tuple(params.pop("result_refs", ()))
    pattern_family = params.pop("pattern_family", "intra_period")
    result = scan_pattern(
        rows,
        pattern_family=pattern_family,
        materiality_floor=params.pop("materiality_floor", 0.03),
        result_refs=result_refs,
        evidence_ref=f"{request.capability_id}:{request.run_id}",
        **params,
    )
    payload = dict(result.typed_payload)
    return CapabilityEvidenceEnvelope(
        evidence_ref=result.evidence_ref,
        capability_id=request.capability_id,
        question_family=request.question_family,
        target_claim=request.target_claim,
        claim_type=request.claim_type,
        metric=request.metric,
        scope=request.scope,
        grain=request.grain,
        baseline_label=str(request.baseline.get("label", "")),
        target_label=str(request.target.get("label", "")),
        time_window=request.time_window,
        numeric_facts={
            "median_uplift": payload.get("median_uplift"),
            "direction_ratio": payload.get("direction_ratio"),
            "comparable_periods": payload.get("comparable_periods"),
        },
        typed_payload=payload,
        result_refs=result_refs,
        sql_hashes=result_refs,
        evidence_type=result.evidence_type,
        strength=result.strength,
        wording_limit=result.wording_limit,
        limitations=tuple(result.limitations),
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_baseline_label": str(request.baseline.get("label", "")),
            "requires_target_label": str(request.target.get("label", "")),
            "requires_evidence_ref": result.evidence_ref,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
    )
```

- [ ] **Step 4: Run harness tests**

Run:

```bash
python3 -m unittest tests.phase4.test_capability_harness
```

Expected: pass.

### Task 5: Compiler And Workflow Wiring

**Files:**
- Modify: `bi_agent/runtime/compiler.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/llm_prompts.py`
- Test: `tests/phase4/test_recipe_registry_and_compiler.py`
- Test: `tests/phase4/test_llm_workflow.py`

- [ ] **Step 1: Add compiler test for registry-backed capability ids**

Add to `tests/phase4/test_recipe_registry_and_compiler.py`:

```python
    def test_compiler_uses_registry_capabilities(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="intra_period",
            requested_nodes=("compare_period_phases", "raw_sql"),
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertIn("compare_period_phases", compiled.mutations.accepted_graph)
        self.assertIn("raw_sql", compiled.mutations.rejected_or_degraded)
```

- [ ] **Step 2: Run compiler test to verify failure**

Run:

```bash
python3 -m unittest tests.phase4.test_recipe_registry_and_compiler
```

Expected: fail because `compare_period_phases` is not accepted yet.

- [ ] **Step 3: Replace duplicated supported-capability constant**

In `bi_agent/runtime/compiler.py`, import registry ids and build supported set:

```python
from bi_agent.runtime.capability_registry import public_capability_ids


REQUIRED_PATTERN_PATHS = (
    "data_quality_profile",
    "compare_period_phases",
    "formula_decompose",
    "event_window_compare",
    "segment_breakdown",
    "outlier_scan",
    "answer_verify",
)

SUPPORTED_CAPABILITIES = frozenset(public_capability_ids())
```

- [ ] **Step 4: Wire LLM prompt inputs**

In `bi_agent/runtime/langgraph_workflow.py`, pass `llm_capability_cards()` and `budget_state.to_llm_summary()` into route planning context. Do not pass caller-provided requested nodes as an LLM hint; route selection must come from the route-planning node output and local compiler validation.

```python
from bi_agent.runtime.capability_registry import llm_capability_cards
from bi_agent.runtime.exploration_budget import default_budget


budget = state.get("budget_state") or default_budget("ordinary")
state["budget_state"] = budget
output = _invoke_llm(
    state,
    "analysis_route",
    {
        "intent": state["intent"],
        "confirmed_understanding": state["confirmed_understanding"],
        "known_capabilities": llm_capability_cards(),
        "budget_state": budget.to_llm_summary(),
    },
)
```

- [ ] **Step 5: Update prompt rule**

In `bi_agent/runtime/llm_prompts.py`, add this sentence to the `analysis_route` instructions:

```python
"Plan only with the provided capability cards. During research mode, do not trade answer quality for cheaper capability paths; use budget state only to avoid unbounded exploration."
```

- [ ] **Step 6: Run workflow/compiler tests**

Run:

```bash
python3 -m unittest tests.phase4.test_recipe_registry_and_compiler tests.phase4.test_llm_workflow
```

Expected: pass.

### Task 6: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run Phase 4 unit tests**

Run:

```bash
python3 -m unittest discover -s tests/phase4
```

Expected: all tests pass.

- [ ] **Step 2: Run contract validators**

Run:

```bash
ruby tools/contracts/validate-contracts.rb
ruby tools/evals/validate-launch-evals.rb
```

Expected: both commands pass.

- [ ] **Step 3: Run the current Phase 4 validation script**

Run:

```bash
python3 tools/phase4/validate_phase4.py
```

Expected: command finishes with fixture and real-data artifacts. Inspect the `custom_q2_vs_q1` artifact from the real 2026H1 suite and confirm the answer binds Q1/Q2 labels from capability evidence rather than generic `baseline/target` labels.

## Self-Review

- Spec coverage: the plan covers LLM-visible capability cards, hidden SQL boundary, public capability catalog, budget defaults, compiler acceptance, workflow route planning, and verifier-ready evidence envelopes.
- Placeholder scan: this plan contains no placeholder implementation steps.
- Type consistency: `CapabilityCard`, `CapabilityRequest`, `CapabilityEvidenceEnvelope`, and `BudgetState` are defined before downstream tasks use them.
