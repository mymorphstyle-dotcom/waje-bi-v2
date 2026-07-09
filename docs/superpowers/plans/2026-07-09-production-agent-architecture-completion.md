# Production Agent Architecture Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the production analytical runtime layer so revenue questions compile into data-aware plans, run flexible ClickHouse diagnostics, preserve analysis assets across turns, and audit final answers without making low-risk wording checks block the user.

**Architecture:** Keep LLM reasoning for business interpretation and final audit. Keep compiler, contracts, SQL safety, permission, and evidence boundaries deterministic. The compiler produces a data-aware runtime plan; ClickHouse planners execute that plan; analysis assets feed the next turn; final answer audit records risk and triggers targeted LLM retry for repairable issues.

**Tech Stack:** Python `unittest`, existing LangGraph workflow module, existing OpenAI-compatible LLM subprocess client, existing ClickHouse runtime, existing Postgres conversation store.

## Global Constraints

- Do not add a new dependency for planning, contract diagnostics, or quality audit.
- All real debugging and eval must enter through `ConversationAgentCore` or Gateway API.
- Do not create local business-answer templates for high-value answer nodes.
- Use LLM calls for business judgment, answer insight, final answer audit, and repairable wording decisions. Local logic is limited to hard boundaries, schema normalization, SQL safety, permission, contract binding, enum validation, and deterministic evidence checks.
- Do not add keyword heuristics to replace business reasoning. If a decision depends on meaning, pass the relevant context, evidence, and failure reason to the LLM.
- LLM retry remains centralized in the subprocess LLM client; individual nodes may pass retry context but must not implement independent retry loops.
- Distinguish `data_absent`, `contract_absent`, `contract_partial`, `permission_blocked`, and `unsupported_grain` before telling the user what is missing.
- Final answer audit is non-blocking for wording and insight issues; hard blockers remain SQL safety, permission, unsupported main claims, and verifier failures.
- Analysis assets from prior turns must become compiler input, not only audit references.
- Keep `artifacts/` as local run output.

---

## Root Cause Notes

### `missing_verified_claim`

**Business meaning:** the final answer did not preserve the auditable verified claim in a way the current checker can recognize. If true, the user may see a polished answer whose main statement cannot be traced back to verified evidence.

**Observed reason in the current system:** the checker is text-oriented. It accepts exact claim text or numeric values, while final summary often paraphrases the claim, changes emphasis, or writes a degraded boundary such as "证据不足，无法可靠归因." That can be a safe answer but still fail the current rule.

**Root cause:** verified claims lack a structured preservation contract. The final answer receives claim text, but the audit checks literal text or numbers instead of structured slots such as metric, scope, baseline, direction, magnitude, evidence strength, and boundary.

**Fix direction:** replace text-preservation checks with an LLM-driven final answer audit that compares structured claim slots and business meaning. Keep hard evidence/verifier failures upstream.

### `missing_business_insight`

**Business meaning:** the answer lacks a useful "so what" for the user: what was ruled in, what was ruled out, where the investigation should go next, and how strong the conclusion is.

**Observed reason in the current system:** the checker searches for fixed phrases like `当前证据能把排查方向收敛到` or `排查方向`. When evidence is weak, the final LLM often writes a conservative limitation paragraph and avoids an insight sentence.

**Root cause:** insight is treated as a string marker, and the answer prompt does not receive a stable business judgment packet that separates confirmed facts, ruled-out paths, candidate mechanisms, and best next check.

**Fix direction:** generate a structured business judgment packet from evidence and compiler assets, then ask the LLM final auditor whether the answer contains a useful interpretation. The audit records missing insight as warning or retry reason, not as a user-visible failed run. Do not replace this with local phrase matching.

---

## File Structure

- Modify `bi_agent/runtime/compiler.py`
  - Continue to own graph acceptance and mutation ledger.
  - Delegate revenue-specific runtime plan construction to `revenue_runtime_plan.py`.

- Create `bi_agent/runtime/revenue_runtime_plan.py`
  - Build runtime plan dictionaries from question text, accepted graph, prior analysis assets, and diagnostic axes.
  - Own revenue windows, baselines, candidate dimensions, capability params, row shapes, and contract gap ids.

- Create `bi_agent/runtime/data_contract_diagnostics.py`
  - Classify each compiler-declared gap as `data_absent`, `contract_absent`, `contract_partial`, `permission_blocked`, `unsupported_grain`, or `unknown`.
  - Produce business-readable owner and repair path.

- Create `bi_agent/runtime/clickhouse_query_planner.py`
  - Convert compiler runtime plan into one or more safe aggregate ClickHouse query specs.
  - Support primary metric, baseline comparison, dimension scan, joint attribution candidates, data quality probes, and event/external-contract probes.

- Modify `bi_agent/runtime/clickhouse_revenue_rows.py`
  - Execute query specs from `clickhouse_query_planner.py`.
  - Return aggregate rows plus diagnostic query refs.

- Create `bi_agent/runtime/analysis_assets.py`
  - Normalize reusable analysis assets: query plans, data availability, contract diagnostics, verified claim slots, eliminated hypotheses, unresolved gaps.

- Modify `bi_agent/conversation/models.py`, `bi_agent/conversation/store.py`, and `bi_agent/conversation/postgres_store.py`
  - Persist analysis assets per topic and expose them to later turns.

- Modify `bi_agent/conversation/runtime.py`
  - Add topic analysis assets into `ConversationRunRequest.context_manifest`.

- Modify `bi_agent/runtime/langgraph_workflow.py`
  - Pass prior analysis assets into compiler.
  - Persist new analysis assets after answer package creation.
  - Replace blocking `answer_quality_gate` behavior with non-blocking final answer audit plus targeted LLM repair when useful.

- Modify `bi_agent/runtime/llm_prompts.py`
  - Add `final_answer_audit` task prompt.
  - Add `final_answer_repair` prompt payload expectations if existing repair task cannot carry audit reasons cleanly.

- Modify `tools/phase7/run_live_conversation_system_test.py`
  - Treat final audit warnings separately from hard failures in strict reports.
  - Keep expectation package semantic checks.

- Add tests:
  - `tests/phase4/test_data_contract_diagnostics.py`
  - `tests/phase4/test_revenue_runtime_plan.py`
  - `tests/phase4/test_clickhouse_query_planner.py`
  - `tests/phase4/test_final_answer_audit.py`
  - `tests/phase7/test_analysis_assets.py`

---

### Task 1: Data-vs-Contract Gap Diagnostics

**Files:**
- Create: `bi_agent/runtime/data_contract_diagnostics.py`
- Test: `tests/phase4/test_data_contract_diagnostics.py`
- Modify: `bi_agent/runtime/answer_package.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`

**Interfaces:**
- Consumes: `compiler_runtime_plan["row_shapes"][*]["contract_gaps"]`, ClickHouse schema fields, optional contract registry records.
- Produces: `diagnose_contract_gaps(...) -> tuple[dict[str, Any], ...]`
- Produces each diagnostic item with keys: `gap_id`, `status`, `data_presence`, `contract_presence`, `owner`, `repair_path`, `claim_effect`.

- [ ] **Step 1: Write failing tests for data exists but contract missing**

Add to `tests/phase4/test_data_contract_diagnostics.py`:

```python
import unittest

from bi_agent.runtime.data_contract_diagnostics import diagnose_contract_gaps


class DataContractDiagnosticsTest(unittest.TestCase):
    def test_field_exists_but_contract_missing_is_contract_absent(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=("payment_status_contract_missing",),
            available_fields=("payment_status", "order_id", "paid_amount_ngn"),
            contract_fields=(),
            permission_denied_fields=(),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["gap_id"], "payment_status_contract_missing")
        self.assertEqual(diagnostics[0]["status"], "contract_absent")
        self.assertEqual(diagnostics[0]["data_presence"], "field_present")
        self.assertEqual(diagnostics[0]["contract_presence"], "missing")
        self.assertIn("补语义合同", diagnostics[0]["repair_path"])

    def test_field_missing_is_data_absent(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=("duplicate_order_contract_missing",),
            available_fields=("paid_amount_ngn",),
            contract_fields=("order_id",),
            permission_denied_fields=(),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["status"], "data_absent")
        self.assertEqual(diagnostics[0]["data_presence"], "field_missing")
        self.assertIn("补数据字段", diagnostics[0]["repair_path"])

    def test_permission_denied_wins_over_contract_missing(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=("high_value_user_contract_missing",),
            available_fields=("user_id", "paid_amount_ngn"),
            contract_fields=("user_id",),
            permission_denied_fields=("user_id",),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["status"], "permission_blocked")
        self.assertEqual(diagnostics[0]["claim_effect"], "block_sensitive_detail_claim")

    def test_unsupported_grain_is_distinct_from_no_data(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=("gameplay_contract_missing",),
            available_fields=("gameplay_id", "paid_amount_ngn"),
            contract_fields=("gameplay_id",),
            permission_denied_fields=(),
            unsupported_grains=("gameplay_id",),
        )

        self.assertEqual(diagnostics[0]["status"], "unsupported_grain")
        self.assertEqual(diagnostics[0]["data_presence"], "field_present")
        self.assertIn("聚合粒度", diagnostics[0]["repair_path"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.phase4.test_data_contract_diagnostics`

Expected: FAIL with `ModuleNotFoundError: No module named 'bi_agent.runtime.data_contract_diagnostics'`.

- [ ] **Step 3: Implement the diagnostic helper**

Create `bi_agent/runtime/data_contract_diagnostics.py`:

```python
from __future__ import annotations

from typing import Any, Iterable


GAP_FIELD_HINTS = {
    "payment_status_contract_missing": ("payment_status", "pay_status", "status"),
    "duplicate_order_contract_missing": ("order_id", "payment_order_id"),
    "high_value_user_contract_missing": ("user_id", "paid_amount_ngn"),
    "gameplay_contract_missing": ("gameplay_id", "gameplay", "play_mode"),
    "event_context_contract_missing": ("event_id", "event_time", "campaign_id"),
}


def diagnose_contract_gaps(
    *,
    contract_gaps: Iterable[str],
    available_fields: Iterable[str],
    contract_fields: Iterable[str],
    permission_denied_fields: Iterable[str],
    unsupported_grains: Iterable[str],
) -> tuple[dict[str, Any], ...]:
    available = {str(field) for field in available_fields}
    contracted = {str(field) for field in contract_fields}
    denied = {str(field) for field in permission_denied_fields}
    unsupported = {str(field) for field in unsupported_grains}
    return tuple(
        _diagnose_gap(str(gap), available, contracted, denied, unsupported)
        for gap in contract_gaps
    )


def _diagnose_gap(
    gap_id: str,
    available: set[str],
    contracted: set[str],
    denied: set[str],
    unsupported: set[str],
) -> dict[str, Any]:
    fields = GAP_FIELD_HINTS.get(gap_id, ())
    present = tuple(field for field in fields if field in available)
    covered = tuple(field for field in fields if field in contracted)
    denied_fields = tuple(field for field in fields if field in denied)
    unsupported_fields = tuple(field for field in fields if field in unsupported)

    if denied_fields:
        return _item(
            gap_id,
            "permission_blocked",
            "field_present" if present else "field_unknown",
            "present" if covered else "missing",
            "权限或安全策略 owner",
            "使用允许的聚合粒度，或由权限 owner 开放对应聚合输出。",
            "block_sensitive_detail_claim",
        )
    if unsupported_fields:
        return _item(
            gap_id,
            "unsupported_grain",
            "field_present" if present else "field_unknown",
            "present" if covered else "partial",
            "语义合同 owner",
            "补充该字段支持的聚合粒度、稀疏阈值和可展示范围。",
            "degrade_to_supported_grain",
        )
    if present and not covered:
        return _item(
            gap_id,
            "contract_absent",
            "field_present",
            "missing",
            "语义合同 owner",
            "补语义合同，声明口径、粒度、刷新规则和可支持 claim。",
            "degrade_claim_strength",
        )
    if present and covered and len(covered) < len(fields):
        return _item(
            gap_id,
            "contract_partial",
            "field_present",
            "partial",
            "语义合同 owner",
            "补齐缺少的合同字段或降级到已覆盖字段。",
            "degrade_claim_strength",
        )
    if not present:
        return _item(
            gap_id,
            "data_absent",
            "field_missing",
            "present" if covered else "missing",
            "数据工程 owner",
            "补数据字段或接入对应事实表，再补语义合同绑定。",
            "block_dependent_claim",
        )
    return _item(
        gap_id,
        "unknown",
        "field_unknown",
        "missing",
        "运行时 owner",
        "检查 schema probe、合同注册和权限绑定是否完整。",
        "degrade_claim_strength",
    )


def _item(
    gap_id: str,
    status: str,
    data_presence: str,
    contract_presence: str,
    owner: str,
    repair_path: str,
    claim_effect: str,
) -> dict[str, Any]:
    return {
        "gap_id": gap_id,
        "status": status,
        "data_presence": data_presence,
        "contract_presence": contract_presence,
        "owner": owner,
        "repair_path": repair_path,
        "claim_effect": claim_effect,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.phase4.test_data_contract_diagnostics`

Expected: OK.

- [ ] **Step 5: Attach diagnostics to Answer Package**

In `bi_agent/runtime/answer_package.py`, add optional parameter:

```python
contract_gap_diagnostics: Optional[Sequence[Mapping[str, Any]]] = None,
```

Normalize it:

```python
contract_gap_diagnostics = () if contract_gap_diagnostics is None else contract_gap_diagnostics
```

Add it to `admin_audit`:

```python
"contract_gap_diagnostics": to_jsonable(contract_gap_diagnostics),
```

In `_build_answer_package_from_state(...)`, pass:

```python
contract_gap_diagnostics=state.get("contract_gap_diagnostics", ()),
```

- [ ] **Step 6: Commit**

```bash
git add bi_agent/runtime/data_contract_diagnostics.py bi_agent/runtime/answer_package.py bi_agent/runtime/langgraph_workflow.py tests/phase4/test_data_contract_diagnostics.py
git commit -m "feat: classify runtime data and contract gaps"
```

---

### Task 2: Deep Revenue Runtime Plan Compiler

**Files:**
- Create: `bi_agent/runtime/revenue_runtime_plan.py`
- Modify: `bi_agent/runtime/compiler.py`
- Test: `tests/phase4/test_revenue_runtime_plan.py`
- Modify: `tests/phase4/test_recipe_registry_and_compiler.py`

**Interfaces:**
- Consumes: accepted graph, diagnostic axes, user question, prior analysis assets.
- Produces: `build_revenue_runtime_plan(...) -> dict[str, Any]`
- Plan contains: `target_metric`, `diagnostic_axes`, `windows`, `baselines`, `dimension_candidates`, `measures`, `capability_params`, `query_intents`, `row_shapes`, `contract_gaps`, `asset_inputs_used`.

- [ ] **Step 1: Write failing tests for windows, dimensions, and capability params**

Add to `tests/phase4/test_revenue_runtime_plan.py`:

```python
import unittest

from bi_agent.runtime.revenue_runtime_plan import build_revenue_runtime_plan


class RevenueRuntimePlanTest(unittest.TestCase):
    def test_multi_baseline_question_compiles_windows_and_params(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("data_quality_profile", "compare_periods", "rolling_window_compare", "driver_decomposition", "answer_verify"),
            diagnostic_axes=("multi_baseline",),
            question_text="相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？",
            prior_assets=(),
        )

        self.assertEqual(plan["windows"]["target"], "yesterday")
        self.assertEqual(
            plan["baselines"],
            ("previous_day", "rolling_7_day_baseline", "same_weekday_last_week"),
        )
        self.assertEqual(
            plan["capability_params"]["rolling_window_compare"]["window_days"],
            7,
        )
        self.assertIn("daily_metric_baselines", plan["query_intents"])

    def test_factor_topk_compiles_dimension_candidates(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "joint_attribution", "driver_decomposition", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="昨天收入变化最大的是哪个一级渠道、地区、设备、包、支付方式或玩法？",
            prior_assets=(),
        )

        dimensions = plan["dimension_candidates"]
        self.assertIn({"field": "channel", "business_name": "一级渠道", "required": True}, dimensions)
        self.assertIn({"field": "payment_method", "business_name": "支付方式", "required": False}, dimensions)
        self.assertEqual(plan["capability_params"]["joint_attribution"]["max_dimension_count"], 2)
        self.assertEqual(plan["capability_params"]["segment_contribution"]["top_k"], 5)

    def test_prior_assets_reduce_repeated_scans(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

        self.assertIn("query:channel-scan", plan["asset_inputs_used"])
        self.assertIn("dimension_scan_reuse", plan["query_intents"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.phase4.test_revenue_runtime_plan`

Expected: FAIL with `ModuleNotFoundError: No module named 'bi_agent.runtime.revenue_runtime_plan'`.

- [ ] **Step 3: Implement the runtime plan builder**

Create `bi_agent/runtime/revenue_runtime_plan.py`:

```python
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


BASE_MEASURES = ("amount", "paid_users", "orders", "first_paid_users")
DIMENSION_CANDIDATES = (
    {"field": "channel", "business_name": "一级渠道", "required": True},
    {"field": "region", "business_name": "地区", "required": False},
    {"field": "device_brand", "business_name": "设备", "required": False},
    {"field": "package_name", "business_name": "包", "required": False},
    {"field": "payment_method", "business_name": "支付方式", "required": False},
    {"field": "gameplay_id", "business_name": "玩法", "required": False},
)


def build_revenue_runtime_plan(
    *,
    target_metric: str,
    accepted_graph: Iterable[str],
    diagnostic_axes: Iterable[str],
    question_text: str,
    prior_assets: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    graph = tuple(dict.fromkeys(str(node) for node in accepted_graph))
    axes = tuple(dict.fromkeys(str(axis) for axis in diagnostic_axes))
    baselines = _baselines(axes, question_text)
    dimensions = _dimensions(graph, axes, question_text)
    row_shape = _row_shape(dimensions, graph, axes)
    asset_refs = _asset_refs(prior_assets)
    query_intents = _query_intents(graph, axes, asset_refs)
    return {
        "target_metric": target_metric,
        "diagnostic_axes": axes,
        "windows": {"target": "yesterday", "history_days": 36},
        "baselines": baselines,
        "dimension_candidates": dimensions,
        "measures": BASE_MEASURES,
        "capability_params": _capability_params(graph, baselines, dimensions),
        "query_intents": query_intents,
        "row_shapes": (row_shape,),
        "contract_gaps": row_shape["contract_gaps"],
        "asset_inputs_used": asset_refs,
    }


def _baselines(axes: tuple[str, ...], question_text: str) -> tuple[str, ...]:
    if "multi_baseline" in axes:
        return ("previous_day", "rolling_7_day_baseline", "same_weekday_last_week")
    if any(token in question_text for token in ("前一天", "昨天", "上涨", "下跌", "变化")):
        return ("previous_day",)
    return ()


def _dimensions(graph: tuple[str, ...], axes: tuple[str, ...], question_text: str) -> tuple[dict[str, Any], ...]:
    if "joint_attribution" in graph or "factor_topk" in axes:
        return DIMENSION_CANDIDATES
    if "segment_contribution" in graph:
        return (DIMENSION_CANDIDATES[0],)
    return ()


def _row_shape(dimensions: tuple[dict[str, Any], ...], graph: tuple[str, ...], axes: tuple[str, ...]) -> dict[str, Any]:
    fields = tuple(item["field"] for item in dimensions)
    gaps = []
    if "gameplay_id" in fields:
        gaps.append("gameplay_contract_missing")
    if "event_impact" in axes:
        gaps.append("event_context_contract_missing")
    if "evidence_quality" in axes:
        gaps.extend(("payment_status_contract_missing", "duplicate_order_contract_missing"))
    if "high_value_user_contribution" in graph:
        gaps.append("high_value_user_contract_missing")
    return {
        "shape_id": "revenue_daily_diagnostic",
        "source": "clickhouse",
        "grain": "business_date_lagos_by_dimension",
        "required_fields": ("period", "group", *BASE_MEASURES),
        "dimension_keys": fields,
        "contract_gaps": tuple(dict.fromkeys(gaps)),
    }


def _capability_params(
    graph: tuple[str, ...],
    baselines: tuple[str, ...],
    dimensions: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if "rolling_window_compare" in graph:
        params["rolling_window_compare"] = {"window_days": 7, "baseline": "rolling_7_day_baseline"}
    if "segment_contribution" in graph:
        params["segment_contribution"] = {"top_k": 5, "min_sample_size": 10}
    if "joint_attribution" in graph:
        params["joint_attribution"] = {
            "max_dimension_count": 2,
            "candidate_dimensions": tuple(item["field"] for item in dimensions),
            "min_sample_size": 10,
        }
    if "compare_periods" in graph:
        params["compare_periods"] = {"baselines": baselines or ("previous_day",)}
    return params


def _asset_refs(prior_assets: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    refs = []
    for asset in prior_assets:
        if asset.get("status") == "usable" and asset.get("query_ref"):
            refs.append(str(asset["query_ref"]))
    return tuple(dict.fromkeys(refs))


def _query_intents(graph: tuple[str, ...], axes: tuple[str, ...], asset_refs: tuple[str, ...]) -> tuple[str, ...]:
    intents = ["daily_metric_baselines"]
    if "dimension_scan_reuse" not in intents and asset_refs:
        intents.append("dimension_scan_reuse")
    if "segment_contribution" in graph:
        intents.append("dimension_scan")
    if "joint_attribution" in graph:
        intents.append("joint_candidate_scan")
    if "data_quality_profile" in graph:
        intents.append("data_quality_probe")
    if "event_impact" in axes:
        intents.append("event_context_probe")
    return tuple(dict.fromkeys(intents))
```

- [ ] **Step 4: Wire compiler to use the builder**

In `bi_agent/runtime/compiler.py`, import:

```python
from bi_agent.runtime.revenue_runtime_plan import build_revenue_runtime_plan
```

Change `compile_graph(...)` signature:

```python
prior_analysis_assets: Iterable[Mapping[str, Any]] = (),
```

Change `_runtime_plan(...)` to call:

```python
return build_revenue_runtime_plan(
    target_metric=target_metric,
    accepted_graph=accepted,
    diagnostic_axes=diagnostic_axes,
    question_text=question_text,
    prior_assets=prior_analysis_assets,
)
```

- [ ] **Step 5: Run tests**

Run:

```bash
python3 -m unittest tests.phase4.test_revenue_runtime_plan
python3 -m unittest tests.phase4.test_recipe_registry_and_compiler
```

Expected: both OK.

- [ ] **Step 6: Commit**

```bash
git add bi_agent/runtime/revenue_runtime_plan.py bi_agent/runtime/compiler.py tests/phase4/test_revenue_runtime_plan.py tests/phase4/test_recipe_registry_and_compiler.py
git commit -m "feat: compile deep revenue runtime plans"
```

---

### Task 3: Flexible ClickHouse Query Planner

**Files:**
- Create: `bi_agent/runtime/clickhouse_query_planner.py`
- Modify: `bi_agent/runtime/clickhouse_revenue_rows.py`
- Test: `tests/phase4/test_clickhouse_query_planner.py`
- Test: `tests/phase4/test_clickhouse_revenue_rows.py`

**Interfaces:**
- Consumes: `compiler_runtime_plan`
- Produces: `build_clickhouse_query_specs(plan: Mapping[str, Any], table: str, run_id: str) -> tuple[dict[str, Any], ...]`
- Query spec keys: `query_id`, `intent`, `sql_text`, `required_fields`, `dimension_keys`, `claim_use`.

- [ ] **Step 1: Write failing tests for dynamic queries**

Add to `tests/phase4/test_clickhouse_query_planner.py`:

```python
import unittest

from bi_agent.runtime.clickhouse_query_planner import build_clickhouse_query_specs


class ClickHouseQueryPlannerTest(unittest.TestCase):
    def test_builds_baseline_and_dimension_scan_queries(self):
        specs = build_clickhouse_query_specs(
            {
                "windows": {"target": "yesterday", "history_days": 36},
                "baselines": ("previous_day", "rolling_7_day_baseline"),
                "query_intents": ("daily_metric_baselines", "dimension_scan"),
                "dimension_candidates": (
                    {"field": "channel", "business_name": "一级渠道", "required": True},
                    {"field": "payment_method", "business_name": "支付方式", "required": False},
                ),
                "row_shapes": (
                    {
                        "required_fields": ("period", "group", "amount", "paid_users", "orders"),
                        "dimension_keys": ("channel", "payment_method"),
                    },
                ),
            },
            table="paid_order_success_clean_20240101_20260704",
            run_id="run-1",
        )

        intents = {spec["intent"] for spec in specs}
        self.assertIn("daily_metric_baselines", intents)
        self.assertIn("dimension_scan", intents)
        self.assertTrue(all("GROUP BY" in spec["sql_text"] for spec in specs))
        self.assertTrue(all("paid_order_success_clean_20240101_20260704" in spec["sql_text"] for spec in specs))

    def test_unsafe_table_returns_no_specs(self):
        specs = build_clickhouse_query_specs(
            {"query_intents": ("daily_metric_baselines",), "row_shapes": ({"required_fields": ("amount",)},)},
            table="paid_order; DROP TABLE x",
            run_id="run-1",
        )

        self.assertEqual(specs, ())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.phase4.test_clickhouse_query_planner`

Expected: FAIL with `ModuleNotFoundError: No module named 'bi_agent.runtime.clickhouse_query_planner'`.

- [ ] **Step 3: Implement query specs**

Create `bi_agent/runtime/clickhouse_query_planner.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bi_agent.runtime.clickhouse_runtime import IDENTIFIER_PATTERN


def build_clickhouse_query_specs(
    plan: Mapping[str, Any],
    *,
    table: str,
    run_id: str,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(table, str) or IDENTIFIER_PATTERN.match(table) is None:
        return ()
    intents = tuple(plan.get("query_intents") or ("daily_metric_baselines",))
    row_shape = _first_row_shape(plan)
    specs = []
    for intent in intents:
        if intent == "daily_metric_baselines":
            specs.append(_daily_metric_query(table, run_id, row_shape))
        elif intent == "dimension_scan":
            specs.append(_dimension_scan_query(table, run_id, row_shape))
        elif intent == "joint_candidate_scan":
            specs.append(_joint_candidate_query(table, run_id, row_shape))
        elif intent == "data_quality_probe":
            specs.append(_data_quality_probe(table, run_id))
    return tuple(spec for spec in specs if spec)


def _first_row_shape(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    shapes = plan.get("row_shapes") or ()
    for shape in shapes:
        if isinstance(shape, Mapping):
            return shape
    return {}


def _daily_metric_query(table: str, run_id: str, row_shape: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "query_id": f"{run_id}:daily_metric_baselines",
        "intent": "daily_metric_baselines",
        "sql_text": f"""
SELECT
    business_date_lagos AS period,
    multiIf(
        business_date_lagos = toDate(now('Africa/Lagos')) - 1, 'target',
        business_date_lagos = toDate(now('Africa/Lagos')) - 2, 'previous_day',
        business_date_lagos >= toDate(now('Africa/Lagos')) - 8, 'rolling_7_day_baseline',
        'history'
    ) AS group,
    sum(paid_amount_ngn) AS amount,
    uniqExact(user_id) AS paid_users,
    count() AS orders,
    countIf(is_first_payment = '1') AS first_paid_users
FROM {table}
WHERE business_date_lagos >= toDate(now('Africa/Lagos')) - 36
  AND business_date_lagos <= toDate(now('Africa/Lagos')) - 1
GROUP BY period, group
LIMIT 5000
""".strip(),
        "required_fields": tuple(row_shape.get("required_fields") or ("period", "group", "amount")),
        "dimension_keys": (),
        "claim_use": "baseline_metric",
    }


def _dimension_scan_query(table: str, run_id: str, row_shape: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = tuple(row_shape.get("dimension_keys") or ("channel",))
    select_dims = ", ".join(dimensions)
    return {
        "query_id": f"{run_id}:dimension_scan",
        "intent": "dimension_scan",
        "sql_text": f"""
SELECT
    business_date_lagos AS period,
    multiIf(
        business_date_lagos = toDate(now('Africa/Lagos')) - 1, 'target',
        business_date_lagos = toDate(now('Africa/Lagos')) - 2, 'baseline',
        'history'
    ) AS group,
    {select_dims},
    sum(paid_amount_ngn) AS amount,
    uniqExact(user_id) AS paid_users,
    count() AS orders
FROM {table}
WHERE business_date_lagos >= toDate(now('Africa/Lagos')) - 36
  AND business_date_lagos <= toDate(now('Africa/Lagos')) - 1
GROUP BY period, group, {select_dims}
LIMIT 5000
""".strip(),
        "required_fields": ("period", "group", "amount", "paid_users", "orders"),
        "dimension_keys": dimensions,
        "claim_use": "segment_or_factor_attribution",
    }


def _joint_candidate_query(table: str, run_id: str, row_shape: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = tuple(row_shape.get("dimension_keys") or ("channel", "payment_method"))[:4]
    select_dims = ", ".join(dimensions)
    return {
        "query_id": f"{run_id}:joint_candidate_scan",
        "intent": "joint_candidate_scan",
        "sql_text": f"""
SELECT
    business_date_lagos AS period,
    multiIf(
        business_date_lagos = toDate(now('Africa/Lagos')) - 1, 'target',
        business_date_lagos = toDate(now('Africa/Lagos')) - 2, 'baseline',
        'history'
    ) AS group,
    {select_dims},
    sum(paid_amount_ngn) AS amount,
    count() AS orders
FROM {table}
WHERE business_date_lagos >= toDate(now('Africa/Lagos')) - 36
  AND business_date_lagos <= toDate(now('Africa/Lagos')) - 1
GROUP BY period, group, {select_dims}
LIMIT 5000
""".strip(),
        "required_fields": ("period", "group", "amount", "orders"),
        "dimension_keys": dimensions,
        "claim_use": "joint_attribution_candidates",
    }


def _data_quality_probe(table: str, run_id: str) -> dict[str, Any]:
    return {
        "query_id": f"{run_id}:data_quality_probe",
        "intent": "data_quality_probe",
        "sql_text": f"""
SELECT
    count() AS orders,
    uniqExact(user_id) AS paid_users,
    min(business_date_lagos) AS min_period,
    max(business_date_lagos) AS max_period
FROM {table}
WHERE business_date_lagos >= toDate(now('Africa/Lagos')) - 36
  AND business_date_lagos <= toDate(now('Africa/Lagos')) - 1
LIMIT 1
""".strip(),
        "required_fields": ("orders", "paid_users", "min_period", "max_period"),
        "dimension_keys": (),
        "claim_use": "data_quality_context",
    }
```

- [ ] **Step 4: Execute specs in `ClickHouseRevenueRows`**

In `bi_agent/runtime/clickhouse_revenue_rows.py`, import:

```python
from bi_agent.runtime.clickhouse_query_planner import build_clickhouse_query_specs
```

Change `plan(...)` to create a first spec when `compiler_runtime_plan` exists:

```python
specs = build_clickhouse_query_specs(
    request.get("compiler_runtime_plan") or {},
    table=self.table,
    run_id=str(request.get("run_id", "run")),
)
if specs:
    first = specs[0]
    return RevenueRowPlan(
        sql_text=first["sql_text"],
        query_id=first["query_id"],
        required_fields=tuple(first["required_fields"]),
        dimension_keys=tuple(first["dimension_keys"]),
    )
```

Keep the existing fallback path for requests without compiler plans.

- [ ] **Step 5: Run tests**

Run:

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_planner
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows
```

Expected: both OK.

- [ ] **Step 6: Commit**

```bash
git add bi_agent/runtime/clickhouse_query_planner.py bi_agent/runtime/clickhouse_revenue_rows.py tests/phase4/test_clickhouse_query_planner.py tests/phase4/test_clickhouse_revenue_rows.py
git commit -m "feat: plan flexible clickhouse revenue diagnostics"
```

---

### Task 4: Analysis Assets Across Turns

**Files:**
- Create: `bi_agent/runtime/analysis_assets.py`
- Modify: `bi_agent/conversation/models.py`
- Modify: `bi_agent/conversation/store.py`
- Modify: `bi_agent/conversation/postgres_store.py`
- Modify: `bi_agent/conversation/runtime.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase7/test_analysis_assets.py`

**Interfaces:**
- Produces: `build_analysis_assets(answer_package: Mapping[str, Any]) -> tuple[dict[str, Any], ...]`
- Store methods:
  - `save_analysis_assets(thread_id: str, topic_id: str, assets: Sequence[Mapping[str, Any]]) -> None`
  - `list_analysis_assets(thread_id: str, topic_id: str) -> tuple[dict[str, Any], ...]`

- [ ] **Step 1: Write failing tests for asset persistence and reuse**

Add to `tests/phase7/test_analysis_assets.py`:

```python
import unittest

from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.analysis_assets import build_analysis_assets


class AnalysisAssetsTest(unittest.TestCase):
    def test_builds_assets_from_answer_package(self):
        assets = build_analysis_assets(
            {
                "admin_audit": {
                    "compiler_runtime_plan": {
                        "query_intents": ("dimension_scan",),
                        "contract_gaps": ("payment_status_contract_missing",),
                    },
                    "contract_gap_diagnostics": (
                        {"gap_id": "payment_status_contract_missing", "status": "contract_absent"},
                    ),
                },
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claim_groups": [
                                {
                                    "text": "当前只能支持渠道贡献候选判断。",
                                    "evidence_refs": ["segment_contribution:inline"],
                                    "strength": "medium",
                                }
                            ]
                        },
                    }
                ],
            }
        )

        asset_types = {asset["asset_type"] for asset in assets}
        self.assertIn("compiler_runtime_plan", asset_types)
        self.assertIn("contract_gap_diagnostic", asset_types)
        self.assertIn("verified_claim_slot", asset_types)

    def test_store_round_trips_topic_assets(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-assets", owner_id="analyst-1")
        topic = store.upsert_topic("thread-assets", title="收入分析")
        store.save_analysis_assets(
            "thread-assets",
            topic.topic_id,
            ({"asset_type": "dimension_scan", "status": "usable", "query_ref": "query:1"},),
        )

        assets = store.list_analysis_assets("thread-assets", topic.topic_id)
        self.assertEqual(assets[0]["query_ref"], "query:1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.phase7.test_analysis_assets`

Expected: FAIL with `ModuleNotFoundError: No module named 'bi_agent.runtime.analysis_assets'`.

- [ ] **Step 3: Implement asset builder**

Create `bi_agent/runtime/analysis_assets.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_analysis_assets(answer_package: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    admin = answer_package.get("admin_audit") or {}
    assets: list[dict[str, Any]] = []
    plan = admin.get("compiler_runtime_plan") or {}
    if plan:
        assets.append({"asset_type": "compiler_runtime_plan", "status": "usable", "payload": plan})
    for item in admin.get("contract_gap_diagnostics") or ():
        if isinstance(item, Mapping):
            assets.append({"asset_type": "contract_gap_diagnostic", "status": item.get("status", "unknown"), "payload": dict(item)})
    for claim in _claim_groups(answer_package):
        assets.append(
            {
                "asset_type": "verified_claim_slot",
                "status": "usable",
                "text": claim.get("text", ""),
                "evidence_refs": tuple(claim.get("evidence_refs") or ()),
                "strength": claim.get("strength"),
            }
        )
    return tuple(assets)


def _claim_groups(answer_package: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    for section in answer_package.get("sections") or ():
        if section.get("section_id") == "summary":
            payload = section.get("payload") or {}
            return tuple(item for item in payload.get("claim_groups") or () if isinstance(item, Mapping))
    return ()
```

- [ ] **Step 4: Add in-memory store methods**

In `bi_agent/conversation/store.py`, add an instance field:

```python
self.analysis_assets: dict[tuple[str, str], list[dict[str, Any]]] = {}
```

Add methods:

```python
def save_analysis_assets(self, thread_id: str, topic_id: str, assets: Sequence[Mapping[str, Any]]) -> None:
    key = (thread_id, topic_id)
    existing = self.analysis_assets.setdefault(key, [])
    existing.extend(dict(asset) for asset in assets)


def list_analysis_assets(self, thread_id: str, topic_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(dict(asset) for asset in self.analysis_assets.get((thread_id, topic_id), ()))
```

- [ ] **Step 5: Wire assets into run context**

In `bi_agent/conversation/runtime.py`, when creating `ConversationRunRequest`, get assets for the current topic:

```python
analysis_assets = (
    self.store.list_analysis_assets(thread_id, topic.topic_id)
    if topic and hasattr(self.store, "list_analysis_assets")
    else ()
)
```

Add to `context_manifest` before passing it to Agent Core:

```python
manifest_dict = manifest.to_dict()
if analysis_assets:
    manifest_dict["analysis_assets"] = list(analysis_assets)
```

Use `manifest_dict` in `ConversationRunRequest(context_manifest=manifest_dict, ...)`.

- [ ] **Step 6: Persist new assets after workflow**

In the Agent Core path that receives a completed `answer_package`, call:

```python
from bi_agent.runtime.analysis_assets import build_analysis_assets

if result.answer_package and topic_id and hasattr(self.store, "save_analysis_assets"):
    self.store.save_analysis_assets(thread_id, topic_id, build_analysis_assets(result.answer_package))
```

Use the existing local variable names in `ConversationAgentCore` or `ConversationRuntime`; keep this write after successful package creation.

- [ ] **Step 7: Run tests**

Run:

```bash
python3 -m unittest tests.phase7.test_analysis_assets
python3 -m unittest discover -s tests/phase7 -p 'test_*.py'
```

Expected: both OK.

- [ ] **Step 8: Commit**

```bash
git add bi_agent/runtime/analysis_assets.py bi_agent/conversation/models.py bi_agent/conversation/store.py bi_agent/conversation/postgres_store.py bi_agent/conversation/runtime.py bi_agent/runtime/langgraph_workflow.py tests/phase7/test_analysis_assets.py
git commit -m "feat: reuse analysis assets across conversation turns"
```

---

### Task 5: LLM-Driven Final Answer Audit

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/llm_prompts.py`
- Test: `tests/phase4/test_final_answer_audit.py`
- Modify: `tests/phase4/test_llm_workflow.py`
- Modify: `tools/phase7/run_live_conversation_system_test.py`

**Interfaces:**
- Produces state key: `final_answer_audit`
- Audit schema:
  - `display_status`: `ready`, `ready_with_warnings`, or `hard_blocked`
  - `hard_blockers`: list of blockers from verifier, permission, SQL safety, or unsupported main claim
  - `repairable_warnings`: list such as `claim_paraphrase_unclear`, `missing_business_interpretation`, `weak_followup`
  - `retry_instruction`: business-language instruction for one targeted final-summary retry
  - `business_audit_summary`: user-safe audit explanation

- [ ] **Step 1: Write failing unit tests for non-blocking warnings**

Add to `tests/phase4/test_final_answer_audit.py`:

```python
import unittest

from bi_agent.runtime.langgraph_workflow import normalize_final_answer_audit


class FinalAnswerAuditTest(unittest.TestCase):
    def test_warning_audit_does_not_block_display(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "ready_with_warnings",
                "hard_blockers": [],
                "repairable_warnings": ["missing_business_interpretation"],
                "retry_instruction": "补一句业务排查方向。",
                "business_audit_summary": "答案可展示，但洞察表达偏弱。",
            }
        )

        self.assertEqual(audit["display_status"], "ready_with_warnings")
        self.assertFalse(audit["blocks_display"])
        self.assertEqual(audit["repairable_warnings"], ["missing_business_interpretation"])

    def test_hard_blocker_blocks_display(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "hard_blocked",
                "hard_blockers": ["unsupported_main_claim"],
                "repairable_warnings": [],
                "retry_instruction": "",
                "business_audit_summary": "主结论越过证据边界。",
            }
        )

        self.assertTrue(audit["blocks_display"])
        self.assertEqual(audit["hard_blockers"], ["unsupported_main_claim"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.phase4.test_final_answer_audit`

Expected: FAIL because `normalize_final_answer_audit` does not exist.

- [ ] **Step 3: Add LLM prompt task**

In `bi_agent/runtime/llm_prompts.py`, add required keys:

```python
"final_answer_audit": (
    "display_status",
    "hard_blockers",
    "repairable_warnings",
    "retry_instruction",
    "business_audit_summary",
),
```

Add task rule:

```python
"final_answer_audit": (
    "Audit whether the final answer can be shown to the user. Use the supplied "
    "verified claim groups, evidence boundaries, final answer, compiler runtime "
    "plan, and prior verifier results. Return hard_blocked only for permission "
    "leak, SQL/security failure, unsupported main claim, or a claim that directly "
    "contradicts verified evidence. Use ready_with_warnings for paraphrase drift, "
    "weak business insight, missing wording anchors, or follow-up quality issues. "
    "Do not require exact wording. retry_instruction must be a concise business "
    "instruction that can be passed into one final-summary retry."
),
```

- [ ] **Step 4: Add normalizer for audit output**

In `bi_agent/runtime/langgraph_workflow.py`, add:

```python
def normalize_final_answer_audit(output: Mapping[str, Any]) -> dict[str, Any]:
    status = str(output.get("display_status") or "ready_with_warnings")
    if status not in {"ready", "ready_with_warnings", "hard_blocked"}:
        status = "ready_with_warnings"
    hard_blockers = [str(item) for item in output.get("hard_blockers") or ()]
    warnings = [str(item) for item in output.get("repairable_warnings") or ()]
    return {
        "display_status": status,
        "blocks_display": status == "hard_blocked" or bool(hard_blockers),
        "hard_blockers": hard_blockers,
        "repairable_warnings": warnings,
        "retry_instruction": str(output.get("retry_instruction") or ""),
        "business_audit_summary": str(output.get("business_audit_summary") or ""),
    }
```

- [ ] **Step 5: Replace blocking quality gate with final answer audit**

In `_answer_quality_gate(...)`, keep follow-up generation and call the LLM audit. Do not gate business insight or claim paraphrase with local phrase matching:

```python
state["follow_up_questions"] = _follow_up_questions(state)
audit = _invoke_llm(
    state,
    "final_answer_audit",
    {
        "user_question": state.get("request", {}).get("question", ""),
        "verified_claims": _verified_claims(state),
        "final_answer": state.get("final_business_summary") or state.get("answer_text", ""),
        "follow_up_questions": state["follow_up_questions"],
        "compiler_runtime_plan": state.get("request", {}).get("compiler_runtime_plan", {}),
        "verifier": state.get("verifier", {}),
        "semantic_audit": state.get("semantic_audit", {}),
        "final_summary_display_warnings": state.get("final_summary_display_warnings", ()),
    },
)
state["final_answer_audit"] = normalize_final_answer_audit(audit)
state["quality_gate"] = {
    "display_status": state["final_answer_audit"]["display_status"],
    "issues": [
        *state["final_answer_audit"]["hard_blockers"],
        *state["final_answer_audit"]["repairable_warnings"],
    ],
    "blocks_display": state["final_answer_audit"]["blocks_display"],
    "final_summary_display_warnings": state.get("final_summary_display_warnings", ()),
}
```

Keep `evaluate_answer_quality(...)` temporarily for dry-run compatibility and existing tests. Stop using it as a display blocker in production workflow.

- [ ] **Step 6: Add targeted retry context**

If `final_answer_audit["retry_instruction"]` is non-empty and there are only repairable warnings, retry `final_business_summary` once by adding this payload key to the existing final summary call:

```python
"final_answer_retry_instruction": state.get("final_answer_audit", {}).get("retry_instruction", ""),
```

The retry still uses `_invoke_llm`, so subprocess timeout and three-attempt retry remain centralized.

- [ ] **Step 7: Update strict eval reporting**

In `tools/phase7/run_live_conversation_system_test.py`, count:

```python
blocks_display = bool((answer_package.get("quality_gate") or {}).get("blocks_display"))
```

Fail strict quality for `blocks_display=True`. Record repairable warnings under `final_answer_audit_warnings` without failing the case.

- [ ] **Step 8: Run tests**

Run:

```bash
python3 -m unittest tests.phase4.test_final_answer_audit
python3 -m unittest tests.phase4.test_llm_workflow
python3 -m unittest tests.phase7.test_agent_core_bridge
```

Expected: all OK.

- [ ] **Step 9: Commit**

```bash
git add bi_agent/runtime/langgraph_workflow.py bi_agent/runtime/llm_prompts.py tests/phase4/test_final_answer_audit.py tests/phase4/test_llm_workflow.py tools/phase7/run_live_conversation_system_test.py
git commit -m "feat: make final answer audit llm driven and non blocking"
```

---

### Task 6: Semantic Eval Expectations

**Files:**
- Modify: `tools/phase7/run_live_conversation_system_test.py`
- Modify: `evals/phase7/conversation_scenarios.yaml`
- Test: `tests/phase7/test_agent_core_bridge.py`

**Interfaces:**
- Strict eval separates:
  - hard failures: missing required capability, verifier failure, permission leak, unsupported main conclusion, SQL failure, missing claim refs.
  - semantic warnings: wording anchor mismatch, weak insight wording, claim paraphrase drift.

- [ ] **Step 1: Write failing test for wording-anchor warning**

Add to `tests/phase7/test_agent_core_bridge.py`:

```python
def test_strict_eval_treats_final_wording_anchor_as_warning(self):
    from tools.phase7.run_live_conversation_system_test import _strict_quality_failed

    turn = {
        "expectation_review": {
            "missing_required_capabilities": [],
            "missing_final_answer_text": ["近 7 日均值"],
            "claim_support_policy_passed": True,
        },
        "answer_package": {
            "quality_gate": {
                "blocks_display": False,
                "issues": ["missing_business_interpretation"],
            }
        },
    }

    self.assertFalse(_strict_quality_failed(turn))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.phase7.test_agent_core_bridge.AgentCoreBridgeTest.test_strict_eval_treats_final_wording_anchor_as_warning`

Expected: FAIL because helper behavior still treats wording anchors as strict failure.

- [ ] **Step 3: Implement strict failure split**

In `tools/phase7/run_live_conversation_system_test.py`, implement:

```python
def _strict_quality_failed(turn: Mapping[str, Any]) -> bool:
    expectation = turn.get("expectation_review") or {}
    package = turn.get("answer_package") or {}
    quality = package.get("quality_gate") or {}
    if expectation.get("missing_required_capabilities"):
        return True
    if expectation.get("claim_support_policy_passed") is False:
        return True
    if quality.get("blocks_display"):
        return True
    return False
```

Record `missing_final_answer_text` into warning output but keep it out of strict failure unless the expectation case explicitly marks that phrase as a hard legal or permission boundary.

- [ ] **Step 4: Run tests and live eval**

Run:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge.AgentCoreBridgeTest.test_strict_eval_treats_final_wording_anchor_as_warning
python3 tools/phase7/run_live_conversation_system_test.py --case paid_amount_revenue_diagnostics_8_question_set --real-llm --real-clickhouse --strict-quality --artifact-dir artifacts/phase7/live-conversation-real-clickhouse-architecture-completion
```

Expected unit test: OK.

Expected live eval: required capabilities remain present; strict failures only occur for hard blockers. Artifact path:

`artifacts/phase7/live-conversation-real-clickhouse-architecture-completion/paid_amount_revenue_diagnostics_8_question_set.json`

- [ ] **Step 5: Commit**

```bash
git add tools/phase7/run_live_conversation_system_test.py evals/phase7/conversation_scenarios.yaml tests/phase7/test_agent_core_bridge.py
git commit -m "test: split hard eval failures from answer warnings"
```

---

## Final Verification

Run:

```bash
python3 -m unittest discover -s tests/phase4 -p 'test_*.py'
python3 -m unittest discover -s tests/phase7 -p 'test_*.py'
python3 -m unittest discover -s tests/phase8 -p 'test_*.py'
python3 tools/phase7/run_live_conversation_system_test.py --case paid_amount_revenue_diagnostics_8_question_set --real-llm --real-clickhouse --strict-quality --artifact-dir artifacts/phase7/live-conversation-real-clickhouse-architecture-completion
```

Expected:

- Phase 4, Phase 7, Phase 8 tests pass.
- Live eval artifact shows `real_clickhouse_verified=true`.
- `missing_required_capabilities` is empty for all 8 revenue turns.
- Contract gaps are classified as data, contract, permission, or grain issues.
- Final answer warnings do not create user-visible failed runs.
- Analysis assets from earlier turns appear in later turn context manifests.

## Self-Review

- Spec coverage: covers data-vs-contract distinction, deep compiler, flexible ClickHouse planner, final answer audit, analysis assets, and semantic eval.
- Placeholder scan: no task uses placeholder markers or vague test instructions.
- Type consistency: runtime plan remains a dictionary to match existing `compiler_runtime_plan`; new helpers use `Mapping[str, Any]` and tuples to match existing runtime patterns.
