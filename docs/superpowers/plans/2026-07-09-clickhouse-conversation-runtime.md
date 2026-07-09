# ClickHouse Conversation Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Phase 7 conversation runtime actually execute ClickHouse-backed revenue diagnostic queries, feed real aggregate rows into existing capabilities, and reduce false `insufficient_evidence` caused by missing row shape or missing multi-dimensional attribution.

**Architecture:** Reuse the existing `ClickHouseRuntime.aggregate()` and existing capability functions. Add one narrow revenue row provider that plans safe aggregate SQL, validates it before execution, fetches bounded aggregate rows, and injects those rows into LangGraph before capability execution. Keep `final_business_summary`; drift remains a warning.

**Tech Stack:** Python 3, LangGraph, existing `ClickHouseRuntime`, existing `validate_select_only`, existing Phase 4/7 unittest harness, ClickHouse table `paid_order_success_clean_20240101_20260704`.

## Global Constraints

- Do not build a full semantic compiler in this pass; use one revenue diagnostics row provider.
- Use existing `ClickHouseRuntime`, `validate_select_only`, and capability functions.
- Every real ClickHouse query must be SELECT-only and aggregate-only before execution.
- No raw user ids, order ids, device ids, or other row-level identifiers leave ClickHouse; only aggregate rows enter capabilities.
- `--real-clickhouse` must mean real ClickHouse rows are used, or the run is blocked with owner and missing inputs.
- Preserve context_manifest, ReuseDecision, evidence/result/artifact/memory refs for supportable claims.
- `final_business_summary` stays in the graph; display drift is warning-only.
- Tests first for every task; commit after each task.
- Keep `artifacts/` local and uncommitted.

---

## Current Root Cause

The current conversation runtime has two gaps:

1. `ConversationAgentCore.from_environment(real_clickhouse=True)` only switches to `PostgresConversationStore.from_env()` and checks ClickHouse env in the harness. It does not inject a ClickHouse-backed workflow runner.
2. `_execute_capabilities()` still does:

```python
rows = state["request"].get("rows") or _default_pattern_rows()
```

So `real_llm_real_clickhouse` can still run on default/sample rows. That makes `driver_decomposition`, `joint_attribution`, `high_value_user_contribution`, `outlier_scan`, and `rolling_window_compare` look unsupported even when ClickHouse has the data.

There is also a capability-shape gap:

```python
if "joint_attribution" in capabilities:
    evidence.append(joint_attribution(rows, segment_evidence=segment, result_refs=query_ref))
```

No `dimension_keys` are passed. If rows do not contain enough comparable dimensions, `joint_attribution` returns `insufficient_evidence`.

---

## File Map

- Create: `bi_agent/runtime/clickhouse_revenue_rows.py`
  - Owns revenue diagnostic SQL planning and bounded ClickHouse row fetching.
  - Produces aggregate rows shaped for existing capability functions.
- Modify: `bi_agent/conversation/agent_core.py`
  - When `real_clickhouse=True`, inject the row provider into workflow requests.
- Modify: `bi_agent/runtime/langgraph_workflow.py`
  - Add a `fetch_runtime_rows` graph node between runtime binding validation and coverage interpretation.
  - Use provider rows instead of `_default_pattern_rows()` when present.
  - Pass `dimension_keys` into `joint_attribution`.
- Modify: `tools/phase7/run_live_conversation_system_test.py`
  - Make `--real-clickhouse` assert row provider usage in artifact metadata.
- Test: `tests/phase7/test_agent_core_bridge.py`
  - ConversationCore wiring and real-clickhouse flag behavior.
- Test: `tests/phase4/test_clickhouse_revenue_rows.py`
  - Query provider SQL safety, row shaping, and failure behavior with fake ClickHouse runtime.
- Test: `tests/phase4/test_llm_workflow.py`
  - LangGraph uses provider rows and passes joint attribution dimensions.

---

### Task 1: ClickHouse Revenue Row Provider

**Files:**
- Create: `bi_agent/runtime/clickhouse_revenue_rows.py`
- Test: `tests/phase4/test_clickhouse_revenue_rows.py`

**Interfaces:**
- Consumes: `ClickHouseRuntime.aggregate(sql: str, query_id: str) -> ClickHouseQueryResult`
- Produces:
  - `RevenueRowPlan(sql_text: str, query_id: str, required_fields: tuple[str, ...], dimension_keys: tuple[str, ...])`
  - `ClickHouseRevenueRows.plan(request: Mapping[str, Any], intent: Mapping[str, Any], accepted_graph: Sequence[str]) -> RevenueRowPlan`
  - `ClickHouseRevenueRows.fetch(plan: RevenueRowPlan) -> RevenueRowsResult`

- [ ] **Step 1: Write failing tests**

Add `tests/phase4/test_clickhouse_revenue_rows.py`:

```python
import unittest

from bi_agent.runtime.clickhouse_revenue_rows import ClickHouseRevenueRows
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult


class FakeRuntime:
    def __init__(self, rows=(), ok=True, reason=""):
        self.rows = tuple(rows)
        self.ok = ok
        self.reason = reason
        self.calls = []
        self.binding = type("Binding", (), {"ok": True, "reason": ""})()

    def configured(self):
        return self.binding.ok

    def aggregate(self, sql, query_id):
        self.calls.append((sql, query_id))
        return ClickHouseQueryResult(
            ok=self.ok,
            reason=self.reason,
            rows=self.rows,
            query_hash="hash-real",
            query_id=query_id,
        )


class ClickHouseRevenueRowsTest(unittest.TestCase):
    def test_plans_aggregate_only_rows_for_driver_and_joint_attribution(self):
        provider = ClickHouseRevenueRows(runtime=FakeRuntime(), table="paid_order_success_clean_20240101_20260704")
        plan = provider.plan(
            {"run_id": "run-1"},
            {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "yesterday",
            },
            ("compare_periods", "driver_decomposition", "joint_attribution"),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertIn("sum(paid_amount_ngn) AS amount", plan.sql_text)
        self.assertIn("uniqExact(user_id) AS paid_users", plan.sql_text)
        self.assertIn("count() AS orders", plan.sql_text)
        self.assertIn("channel", plan.dimension_keys)
        self.assertIn("payment_method", plan.dimension_keys)
        self.assertIn("amount", plan.required_fields)

    def test_fetch_returns_bounded_aggregate_rows_and_query_ref(self):
        runtime = FakeRuntime(rows=({"period": "2026-07-08", "group": "target", "amount": 120.0},))
        provider = ClickHouseRevenueRows(runtime=runtime, table="paid_order_success_clean_20240101_20260704")
        plan = provider.plan({"run_id": "run-1"}, {"time_window": "yesterday"}, ("compare_periods",))
        result = provider.fetch(plan)

        self.assertTrue(result.ok)
        self.assertEqual(result.rows[0]["amount"], 120.0)
        self.assertEqual(result.query_id, plan.query_id)
        self.assertEqual(result.result_refs, ("hash-real",))

    def test_fetch_blocks_when_runtime_query_fails(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(ok=False, reason="clickhouse_query_failed"),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan({"run_id": "run-1"}, {"time_window": "yesterday"}, ("compare_periods",))
        result = provider.fetch(plan)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "clickhouse_query_failed")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows
```

Expected: fail with `ModuleNotFoundError: No module named 'bi_agent.runtime.clickhouse_revenue_rows'`.

- [ ] **Step 3: Implement provider**

Create `bi_agent/runtime/clickhouse_revenue_rows.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping, Optional, Sequence

from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime
from bi_agent.runtime.sql_safety import validate_select_only


DEFAULT_TABLE = "paid_order_success_clean_20240101_20260704"
MAX_ROWS = 5000


@dataclass(frozen=True)
class RevenueRowPlan:
    sql_text: str
    query_id: str
    required_fields: tuple[str, ...]
    dimension_keys: tuple[str, ...]


@dataclass(frozen=True)
class RevenueRowsResult:
    ok: bool
    rows: tuple[dict[str, Any], ...] = ()
    reason: str = ""
    query_hash: str = ""
    query_id: str = ""
    result_refs: tuple[str, ...] = ()


class ClickHouseRevenueRows:
    def __init__(self, runtime: Optional[ClickHouseRuntime] = None, table: Optional[str] = None) -> None:
        self.runtime = runtime or ClickHouseRuntime.from_env()
        self.table = table or os.environ.get("WAJE_CLICKHOUSE_PAYMENT_TABLE", DEFAULT_TABLE)

    @classmethod
    def from_env(cls) -> "ClickHouseRevenueRows":
        return cls(ClickHouseRuntime.from_env())

    def configured(self) -> bool:
        return self.runtime.configured()

    def binding_reason(self) -> str:
        return self.runtime.binding.reason

    def plan(
        self,
        request: Mapping[str, Any],
        intent: Mapping[str, Any],
        accepted_graph: Sequence[str],
    ) -> RevenueRowPlan:
        dimensions = _dimension_keys(accepted_graph)
        select_dimensions = ", ".join(dimensions)
        group_dimensions = ", ".join(dimensions)
        dimension_sql = f", {select_dimensions}" if select_dimensions else ""
        group_sql = f", {group_dimensions}" if group_dimensions else ""
        query_id = f"{request.get('run_id', 'run')}:clickhouse_revenue_rows"
        sql = f"""
SELECT
    business_date_lagos AS period,
    multiIf(
        business_date_lagos = toDate(now('Africa/Lagos')) - 1, 'target',
        business_date_lagos = toDate(now('Africa/Lagos')) - 2, 'baseline',
        'history'
    ) AS group,
    sum(paid_amount_ngn) AS amount,
    uniqExact(user_id) AS paid_users,
    count() AS orders,
    countIf(is_first_payment = '1') AS first_paid_users{dimension_sql}
FROM {self.table}
WHERE business_date_lagos >= toDate(now('Africa/Lagos')) - 36
  AND business_date_lagos <= toDate(now('Africa/Lagos')) - 1
GROUP BY period, group{group_sql}
LIMIT {MAX_ROWS}
"""
        required = ("period", "group", "amount", "paid_users", "orders")
        return RevenueRowPlan(
            sql_text=sql.strip(),
            query_id=query_id,
            required_fields=required,
            dimension_keys=dimensions,
        )

    def fetch(self, plan: RevenueRowPlan) -> RevenueRowsResult:
        validation = validate_select_only(plan.sql_text, aggregate=True)
        if not validation.ok:
            return RevenueRowsResult(ok=False, reason=validation.reason, query_hash=validation.query_hash, query_id=plan.query_id)
        result = self.runtime.aggregate(plan.sql_text, query_id=plan.query_id)
        if not result.ok:
            return RevenueRowsResult(ok=False, reason=result.reason, query_hash=result.query_hash or validation.query_hash, query_id=result.query_id or plan.query_id)
        rows = tuple(dict(row) for row in result.rows)
        return RevenueRowsResult(
            ok=True,
            rows=rows,
            query_hash=result.query_hash or validation.query_hash,
            query_id=result.query_id or plan.query_id,
            result_refs=(result.query_hash or validation.query_hash,),
        )


def _dimension_keys(accepted_graph: Sequence[str]) -> tuple[str, ...]:
    if "joint_attribution" in accepted_graph:
        return ("channel", "payment_method", "region", "device_brand")
    if "segment_contribution" in accepted_graph:
        return ("channel",)
    return ()
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add bi_agent/runtime/clickhouse_revenue_rows.py tests/phase4/test_clickhouse_revenue_rows.py
git commit -m "feat: add clickhouse revenue row provider"
```

---

### Task 2: Wire Provider Into Conversation Runtime

**Files:**
- Modify: `bi_agent/conversation/agent_core.py`
- Test: `tests/phase7/test_agent_core_bridge.py`

**Interfaces:**
- Consumes: `ClickHouseRevenueRows.from_env()`
- Produces: workflow request key `runtime.row_provider`

- [ ] **Step 1: Write failing tests**

Add to `tests/phase7/test_agent_core_bridge.py`:

```python
def test_real_clickhouse_from_environment_injects_row_provider(self):
    captured = {}

    def runner(request):
        captured.update(request)
        return WorkflowRunResult(
            status="draft",
            run_id=request["run_id"],
            answer_package=_answer_package_with_quality_gate(),
        )

    core = ConversationAgentCore(
        InMemoryConversationStore(),
        workflow_runner=runner,
        conversation_llm_client=FakeConversationLLM(),
        row_provider=object(),
    )
    result = core.run_message(
        thread_id="thread-real-clickhouse-provider",
        user_message="昨天付费金额为什么变了？",
        permission_context={"role": "analyst"},
    )

    self.assertEqual(result["status"], "completed")
    self.assertIn("runtime", captured)
    self.assertIs(captured["runtime"]["row_provider"], core.row_provider)
```

If helper names differ in the file, reuse the existing fake package helper instead of creating a new one.

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge.ConversationAgentCoreBridgeTest.test_real_clickhouse_from_environment_injects_row_provider
```

Expected: fail because `ConversationAgentCore.__init__` has no `row_provider` parameter.

- [ ] **Step 3: Implement minimal injection**

Modify `bi_agent/conversation/agent_core.py`:

```python
class ConversationAgentCore:
    def __init__(self, store, *, workflow_runner=None, conversation_llm_client=None, row_provider=None):
        self.store = store
        self.workflow_runner = workflow_runner or run_pattern_workflow
        self.conversation_llm_client = conversation_llm_client
        self.row_provider = row_provider
```

In `run_message()` before `self.workflow_runner(request)`:

```python
if self.row_provider is not None:
    request.setdefault("runtime", {})["row_provider"] = self.row_provider
```

In `from_environment()`:

```python
row_provider = None
if real_clickhouse:
    from bi_agent.runtime.clickhouse_revenue_rows import ClickHouseRevenueRows
    row_provider = ClickHouseRevenueRows.from_env()
return cls(
    PostgresConversationStore.from_env(),
    conversation_llm_client=_conversation_llm_from_env() if real_llm else None,
    row_provider=row_provider,
)
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add bi_agent/conversation/agent_core.py tests/phase7/test_agent_core_bridge.py
git commit -m "feat: inject clickhouse rows into conversation runtime"
```

---

### Task 3: Fetch ClickHouse Rows Inside LangGraph

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Consumes: `state["request"]["runtime"]["row_provider"]`
- Produces:
  - `state["row_query_plan"]`
  - `state["request"]["rows"]`
  - `state["request"]["required_fields"]`
  - `state["request"]["runtime_rows_source"] = "clickhouse"`
  - `state["request"]["joint_dimension_keys"]`

- [ ] **Step 1: Write failing test**

Add to `tests/phase4/test_llm_workflow.py`:

```python
def test_workflow_uses_clickhouse_provider_rows_instead_of_default_rows(self):
    class Provider:
        def __init__(self):
            self.planned = False
            self.fetched = False

        def configured(self):
            return True

        def binding_reason(self):
            return ""

        def plan(self, request, intent, accepted_graph):
            from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowPlan
            self.planned = True
            return RevenueRowPlan(
                sql_text="SELECT period, group, sum(amount) AS amount FROM t GROUP BY period, group",
                query_id="query-real",
                required_fields=("period", "group", "amount"),
                dimension_keys=("channel", "payment_method"),
            )

        def fetch(self, plan):
            from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowsResult
            self.fetched = True
            return RevenueRowsResult(
                ok=True,
                rows=(
                    {"period": "2026-07-07", "group": "baseline", "amount": 100, "channel": "A", "payment_method": "M"},
                    {"period": "2026-07-08", "group": "target", "amount": 130, "channel": "A", "payment_method": "M"},
                ),
                query_hash="hash-real",
                query_id="query-real",
                result_refs=("hash-real",),
            )

    provider = Provider()
    result = run_pattern_workflow(
        {
            "run_id": "clickhouse-provider-rows",
            "llm_client": FakeLLMClient({"analysis_route": {"requested_nodes": ["compare_periods", "joint_attribution", "answer_verify"]}}),
            "runtime": {"row_provider": provider},
        }
    )

    self.assertEqual(result.status, "draft")
    self.assertTrue(provider.planned)
    self.assertTrue(provider.fetched)
    self.assertIn("hash-real", result.answer_package["sections"][1]["payload"]["evidence"][0]["result_refs"])
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m unittest tests.phase4.test_llm_workflow.LLMWorkflowTest.test_workflow_uses_clickhouse_provider_rows_instead_of_default_rows
```

Expected: fail because workflow never calls the provider.

- [ ] **Step 3: Add graph node**

Modify graph in `bi_agent/runtime/langgraph_workflow.py`:

```python
("fetch_runtime_rows", _fetch_runtime_rows),
```

Route:

```python
graph.add_conditional_edges(
    "validate_runtime_binding",
    _route_after_runtime_binding,
    {"valid": "fetch_runtime_rows", "block": "generate_blocked_explanation"},
)
graph.add_conditional_edges(
    "fetch_runtime_rows",
    _route_after_runtime_rows,
    {"valid": "interpret_data_coverage", "block": "generate_blocked_explanation"},
)
```

Implement:

```python
def _fetch_runtime_rows(state: WorkflowState) -> WorkflowState:
    provider = state["request"].get("runtime", {}).get("row_provider")
    if provider is None:
        return state
    if not provider.configured():
        state.setdefault("validator_results", []).append(
            {"validator": "clickhouse_runtime", "ok": False, "reason": provider.binding_reason()}
        )
        return state
    plan = provider.plan(
        state["request"],
        state["intent"],
        tuple(state["compiled_graph"].mutations.accepted_graph),
    )
    state["row_query_plan"] = {
        "sql_text": plan.sql_text,
        "query_id": plan.query_id,
        "required_fields": list(plan.required_fields),
        "dimension_keys": list(plan.dimension_keys),
    }
    state["sql_text"] = plan.sql_text
    state["request"]["required_fields"] = tuple(plan.required_fields)
    result = provider.fetch(plan)
    if not result.ok:
        state.setdefault("validator_results", []).append(
            {"validator": "clickhouse_query", "ok": False, "reason": result.reason, "sql_hash": result.query_hash}
        )
        return state
    state["request"]["rows"] = [dict(row) for row in result.rows]
    state["request"]["runtime_rows_source"] = "clickhouse"
    state["request"]["joint_dimension_keys"] = tuple(plan.dimension_keys)
    state["sql_hash"] = result.query_hash
    state["request"]["result_refs"] = tuple(result.result_refs)
    return state


def _route_after_runtime_rows(state: WorkflowState) -> str:
    if any(not result.get("ok", True) for result in state.get("validator_results", ())):
        return "block"
    return "valid"
```

Update `_execute_capabilities()`:

```python
query_ref = tuple(state["request"].get("result_refs") or (state["sql_hash"],))
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m unittest tests.phase4.test_llm_workflow.LLMWorkflowTest.test_workflow_uses_clickhouse_provider_rows_instead_of_default_rows
python3 -m unittest discover -s tests/phase4 -p 'test_*.py'
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add bi_agent/runtime/langgraph_workflow.py tests/phase4/test_llm_workflow.py
git commit -m "feat: fetch clickhouse rows in langgraph workflow"
```

---

### Task 4: Pass Multi-Dimensional Parameters To Attribution Capabilities

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Consumes: `request["joint_dimension_keys"]`
- Produces: `joint_attribution(..., dimension_keys=...)`

- [ ] **Step 1: Write failing test**

Add to `tests/phase4/test_llm_workflow.py`:

```python
def test_joint_attribution_uses_clickhouse_dimension_keys(self):
    result = run_pattern_workflow(
        {
            "run_id": "joint-dimensions",
            "llm_client": FakeLLMClient({"analysis_route": {"requested_nodes": ["joint_attribution", "answer_verify"]}}),
            "requested_nodes": ["joint_attribution", "answer_verify"],
            "joint_dimension_keys": ("channel", "payment_method"),
            "rows": [
                {"period": "p1", "group": "baseline", "amount": 100, "channel": "A", "payment_method": "M", "n": 50},
                {"period": "p1", "group": "target", "amount": 150, "channel": "A", "payment_method": "M", "n": 50},
                {"period": "p1", "group": "baseline", "amount": 80, "channel": "B", "payment_method": "N", "n": 50},
                {"period": "p1", "group": "target", "amount": 70, "channel": "B", "payment_method": "N", "n": 50},
            ],
        }
    )

    evidence = result.answer_package["sections"][1]["payload"]["evidence"]
    joint = next(item for item in evidence if item["capability_id"] == "joint_attribution")
    self.assertEqual(joint["typed_payload"]["dimension_keys"], ["channel", "payment_method"])
    self.assertEqual(joint["evidence_type"], "statistical_association")
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m unittest tests.phase4.test_llm_workflow.LLMWorkflowTest.test_joint_attribution_uses_clickhouse_dimension_keys
```

Expected: fail because `joint_attribution` is called without `dimension_keys`.

- [ ] **Step 3: Implement dimension passthrough**

Modify `bi_agent/runtime/langgraph_workflow.py`:

```python
def _joint_attribution_params(state: WorkflowState) -> dict[str, Any]:
    params = dict(state.get("intent", {}).get("pattern_params", {}))
    dimensions = tuple(
        state["request"].get("joint_dimension_keys")
        or params.get("joint_dimension_keys")
        or params.get("dimension_keys")
        or ()
    )
    return {
        "dimension_keys": dimensions,
        "group_key": params.get("group_key", "group"),
        "target_group": params.get("target_group", "target"),
        "baseline_group": params.get("baseline_group", "baseline"),
        "amount_key": params.get("amount_key", "amount"),
    }
```

Call:

```python
evidence.append(
    joint_attribution(
        rows,
        segment_evidence=segment,
        result_refs=query_ref,
        **_joint_attribution_params(state),
    )
)
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m unittest tests.phase4.test_llm_workflow.LLMWorkflowTest.test_joint_attribution_uses_clickhouse_dimension_keys
python3 -m unittest discover -s tests/phase4 -p 'test_*.py'
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add bi_agent/runtime/langgraph_workflow.py tests/phase4/test_llm_workflow.py
git commit -m "feat: pass dimensions into joint attribution"
```

---

### Task 5: Make Phase 7 Real Eval Prove ClickHouse Usage

**Files:**
- Modify: `tools/phase7/run_live_conversation_system_test.py`
- Test: `tests/phase7/test_agent_core_bridge.py`

**Interfaces:**
- Consumes: `answer_package.sections[].payload.evidence[].result_refs`
- Produces: artifact fields `real_clickhouse_verified`, `clickhouse_result_refs`

- [ ] **Step 1: Write failing test**

Add to `tests/phase7/test_agent_core_bridge.py`:

```python
def test_live_harness_marks_real_clickhouse_unverified_without_clickhouse_refs(self):
    from tools.phase7.run_live_conversation_system_test import _real_clickhouse_review

    review = _real_clickhouse_review(
        {
            "answer_package": {
                "sections": [
                    {"section_id": "evidence", "payload": {"evidence": [{"result_refs": ["fixture-hash"]}]}}
                ]
            }
        },
        real_clickhouse=True,
    )

    self.assertFalse(review["real_clickhouse_verified"])
    self.assertIn("missing_clickhouse_result_refs", review["issues"])
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge.AgentCoreBridgeTest.test_live_harness_marks_real_clickhouse_unverified_without_clickhouse_refs
```

Expected: fail because `_real_clickhouse_review` does not exist.

- [ ] **Step 3: Implement artifact review**

In `tools/phase7/run_live_conversation_system_test.py` add:

```python
def _real_clickhouse_review(result: dict[str, Any], *, real_clickhouse: bool) -> dict[str, Any]:
    if not real_clickhouse:
        return {"required": False, "real_clickhouse_verified": True, "issues": []}
    refs = []
    package = result.get("answer_package") or {}
    for section in package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        for item in payload.get("evidence", []) if isinstance(payload.get("evidence"), list) else []:
            refs.extend(str(ref) for ref in item.get("result_refs", []) if ref)
    verified = any(ref and ref != "fixture-hash" and not ref.startswith("phase4-draft") for ref in refs)
    return {
        "required": True,
        "real_clickhouse_verified": verified,
        "clickhouse_result_refs": sorted(set(refs)),
        "issues": [] if verified else ["missing_clickhouse_result_refs"],
    }
```

Add review output to each turn record and case aggregate. In strict mode, fail the case if `real_clickhouse=True` and review is not verified.

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge
```

Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add tools/phase7/run_live_conversation_system_test.py tests/phase7/test_agent_core_bridge.py
git commit -m "test: require real clickhouse refs in phase7 eval"
```

---

### Task 6: Live 8-Question Eval Acceptance

**Files:**
- No code file changes unless tests reveal a general bug.
- Artifact output: `artifacts/phase7/live-conversation-real-clickhouse-wired/paid_amount_revenue_diagnostics_8_question_set.json`

**Interfaces:**
- Consumes: `.env` ClickHouse and LLM variables.
- Produces: real eval artifact with `real_clickhouse_verified=true`.

- [ ] **Step 1: Run local tests**

Run:

```bash
python3 -m unittest discover -s tests/phase4 -p 'test_*.py'
python3 -m unittest discover -s tests/phase7 -p 'test_*.py'
python3 -m unittest discover -s tests/phase8 -p 'test_*.py'
```

Expected:

```text
OK
OK
OK
```

- [ ] **Step 2: Run real eval**

Run:

```bash
python3 tools/phase7/run_live_conversation_system_test.py \
  --case paid_amount_revenue_diagnostics_8_question_set \
  --real-llm \
  --real-clickhouse \
  --strict-quality \
  --artifact-dir artifacts/phase7/live-conversation-real-clickhouse-wired
```

Expected:

- Every turn status is `completed`, `waiting_for_clarification` with resumed completed, or business `blocked` with answer package.
- `real_clickhouse_verified=true`.
- `claim_evidence_review.passed=true` for every completed turn.
- Missing capability count decreases for:
  - `joint_attribution`
  - `high_value_user_contribution`
  - `outlier_scan`
  - `rolling_window_compare`
- If strict eval still fails, failures must be classified as:
  - planner capability gap
  - ClickHouse field/contract gap
  - final summary warning
  - eval expectation mismatch

- [ ] **Step 3: Write review note**

Create `docs/reviews/phase7-clickhouse-conversation-runtime-YYYYMMDD.md` with:

```markdown
# Phase 7 ClickHouse Conversation Runtime Review

## Result

- Artifact: `artifacts/phase7/live-conversation-real-clickhouse-wired/paid_amount_revenue_diagnostics_8_question_set.json`
- User-facing completed turns:
- Strict eval passed turns:
- Quality-gate passed turns:
- Real ClickHouse verified:

## Remaining Gaps

| Gap | Evidence | Owner | Next Step |
|---|---|---|---|
```

- [ ] **Step 4: Commit review**

```bash
git add docs/reviews/phase7-clickhouse-conversation-runtime-YYYYMMDD.md
git commit -m "docs: review clickhouse conversation runtime eval"
```

---

## Expected Effect

- Current product-facing result should stay at `8/8 completed`.
- Strict eval should improve from `1/8`, but this plan does not fake a target number.
- The main expected quality improvement is fewer vague “证据不足/无法归因” answers when ClickHouse contains the needed aggregate fields.
- Remaining “证据不足” after this plan is meaningful: it should point to actual field/contract gaps, missing event data, unsupported grain, or planner capability gaps.

## Self-Review

- Spec coverage: ClickHouse runtime wiring, row-source proof, capability attribution shape, live eval artifact, and review note are covered.
- Placeholder scan: no placeholder markers or open-ended test steps remain.
- Type consistency: `RevenueRowPlan`, `RevenueRowsResult`, and `ClickHouseRevenueRows` are defined before downstream tasks consume them.
