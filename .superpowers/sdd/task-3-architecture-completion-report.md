# Task 3 Architecture Completion Report

## Scope

Task 3 adds a deterministic ClickHouse query planner that reads `compiler_runtime_plan`, emits safe query specs, and lets `ClickHouseRevenueRows` consume those specs before falling back to the older accepted-graph path.

Implemented files:

- `bi_agent/runtime/clickhouse_query_planner.py`
- `bi_agent/runtime/clickhouse_revenue_rows.py`
- `tests/phase4/test_clickhouse_query_planner.py`
- `tests/phase4/test_clickhouse_revenue_rows.py`

## TDD Record

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_planner
```

Observed failure:

```text
ModuleNotFoundError: No module named 'bi_agent.runtime.clickhouse_query_planner'
```

Command:

```bash
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows.ClickHouseRevenueRowsTest.test_plan_uses_compiler_query_specs_before_graph_fallback
```

Observed failure:

```text
AssertionError: 'run-compiler-plan:clickhouse_revenue_rows' != 'run-compiler-plan:dimension_scan'
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_planner
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows
python3 -m unittest tests.phase4.test_revenue_runtime_plan tests.phase4.test_recipe_registry_and_compiler
python3 -m unittest tests.phase7.test_agent_core_bridge
python3 -m unittest tests.phase4.test_llm_workflow
```

Observed result:

```text
Ran 2 tests in 0.000s
OK

Ran 6 tests in 0.003s
OK

Ran 29 tests in 0.006s
OK

Ran 32 tests in 0.356s
OK

Ran 150 tests in 5.619s
OK
```

## What Changed

### 1. Added a dedicated ClickHouse query-spec builder

`bi_agent/runtime/clickhouse_query_planner.py` now owns query planning from runtime metadata.

Current planner behavior:

- validates table and dimension identifiers with `IDENTIFIER_PATTERN`
- reads `query_intents`, `row_shapes`, `dimension_candidates`, `baselines`, `windows`, and `capability_params`
- emits structured query specs with:
  - `query_id`
  - `intent`
  - `sql_text`
  - `required_fields`
  - `dimension_keys`
  - `claim_use`

Supported deterministic intents:

- `daily_metric_baselines`
- `dimension_scan`
- `joint_candidate_scan`
- `data_quality_probe`

### 2. Moved dimension and baseline selection onto runtime-plan metadata

The query planner stops deriving scan shape from accepted-graph shortcuts.

It now uses:

- `row_shapes[*].dimension_keys`
- `row_shapes[*].required_fields`
- `capability_params["joint_attribution"]["max_dimension_count"]`
- `windows["history_days"]`
- `baselines`

That keeps SQL shape aligned with the compiler output introduced in Task 2.

### 3. Wired `ClickHouseRevenueRows` to prefer compiler query specs

`ClickHouseRevenueRows.plan(...)` now:

- checks whether `compiler_runtime_plan` carries explicit `query_intents`
- builds query specs through `build_clickhouse_query_specs(...)`
- returns the first emitted spec as the runtime `RevenueRowPlan`

The older fallback path stays in place for requests that only carry row-shape hints or older graph-only inputs. That preserves Task 2 behavior and avoids breaking current callers during the rollout.

### 4. Added direct planner coverage and integration coverage

`tests/phase4/test_clickhouse_query_planner.py` covers:

- baseline plus dimension-scan query generation
- identifier blocking for unsafe table names

`tests/phase4/test_clickhouse_revenue_rows.py` now also covers:

- compiler query-spec consumption before graph fallback
- preservation of the older row-shape-only fallback path

## Constraint Check

- No new dependency added
- No Task 1 or Task 2 behavior reverted
- SQL safety still runs through identifier checks plus `validate_select_only(...)`
- No local business-answer template added
- No keyword heuristics added for business reasoning
- LLM retry behavior unchanged
- `artifacts/` behavior unchanged

## Concerns

- The current planner supports the relative daily baseline intents already expressed in runtime plans. Explicit custom-period labels such as quarter names, plus `event_context_probe`, still need a later contract for deterministic date binding before they can become executable SQL without guessing.

## Reviewer Follow-up Fixes

This section supersedes the earlier note that `ClickHouseRevenueRows.plan(...)` returns the first emitted compiler spec. It now selects the spec that matches the accepted graph, and it preserves blocked/non-executable reasons when the runtime plan does not carry enough deterministic metadata.

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_planner tests.phase4.test_clickhouse_revenue_rows
```

Observed failures before the fix:

```text
ERROR: test_custom_baseline_with_explicit_ranges_builds_deterministic_query
KeyError: 'reason'

FAIL: test_custom_baseline_without_bound_dates_returns_blocked_reason
AssertionError: expected blocked spec, got relative yesterday/history SQL

FAIL: test_plan_prefers_joint_scan_when_multi_intent_graph_needs_dimensions
AssertionError: 'run-multi-intent:daily_metric_baselines' != 'run-multi-intent:joint_candidate_scan'

FAIL: test_plan_blocks_unbound_custom_baseline_windows
AssertionError: expected empty sql_text, got relative baseline SQL

FAIL: test_plan_keeps_dimension_scan_reuse_non_executable
AssertionError: expected empty sql_text, got baseline SQL

FAIL: test_plan_keeps_event_probe_non_executable_without_binding
AssertionError: expected empty sql_text, got baseline SQL
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_planner tests.phase4.test_clickhouse_revenue_rows
python3 -m unittest tests.phase4.test_clickhouse_query_planner tests.phase4.test_clickhouse_revenue_rows tests.phase4.test_revenue_runtime_plan tests.phase4.test_recipe_registry_and_compiler
python3 -m unittest tests.phase7.test_agent_core_bridge
python3 -m unittest tests.phase4.test_llm_workflow
```

Observed result:

```text
Ran 14 tests in 0.003s
OK

Ran 41 tests in 0.011s
OK

Ran 32 tests in 0.367s
OK

Ran 150 tests in 5.811s
OK
```

### What Changed In This Follow-up

- `build_clickhouse_query_specs(...)` now keeps explicit non-executable specs with `reason` for `dimension_scan_reuse`, `event_context_probe`, and unbound custom-baseline windows.
- Custom-baseline SQL only compiles when runtime metadata carries deterministic date ranges through `windows.target` / `windows.baseline` range strings or explicit `target_start` / `target_end` and `baseline_start` / `baseline_end`.
- `ClickHouseRevenueRows.plan(...)` no longer consumes `specs[0]`; it selects the spec that fits the accepted graph, so joint or segment plans keep dimension-bearing scans instead of dropping to baseline rows.
- When the accepted graph requires reuse-only or unbound event/custom-baseline behavior, the row plan now returns `sql_text=""` plus the planner reason, which prevents unrelated fresh SQL from running.
- Added regression coverage for multi-intent spec selection, deterministic custom-baseline compilation, blocked custom baselines, reuse-only plans, and event-probe plans.

### Follow-up Concerns

- Custom-baseline execution is deterministic only when runtime plan metadata includes concrete date ranges. Label-only windows such as `Q1` / `Q2` remain intentionally blocked with `custom_baseline_window_unbound`.
- `event_context_probe` remains non-executable until the runtime plan carries an explicit event-binding contract. That is deliberate; this patch avoids fabricating SQL from incomplete metadata.

## Critical Finding Fix: Explicit Compiler Plans Must Not Fall Back

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_planner tests.phase4.test_clickhouse_revenue_rows
```

Observed failures before the fix:

```text
FAIL: test_dimension_scan_with_unsafe_row_shape_dimensions_returns_blocked_reason
AssertionError: 0 != 1

FAIL: test_plan_with_explicit_dimension_scan_and_empty_dimensions_stays_blocked
AssertionError: expected empty sql_text, got fallback baseline SQL

FAIL: test_plan_with_unsafe_compiler_dimension_does_not_emit_dimension_sql
AssertionError: expected empty sql_text, got fallback baseline SQL
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_planner tests.phase4.test_clickhouse_revenue_rows
python3 -m unittest tests.phase4.test_revenue_runtime_plan tests.phase4.test_recipe_registry_and_compiler
```

Observed result:

```text
Ran 17 tests in 0.002s
OK

Ran 27 tests in 0.006s
OK
```

### What Changed In This Critical Fix

- `build_clickhouse_query_specs(...)` now turns `dimension_scan` and `joint_candidate_scan` with empty dimension keys into blocked specs with `reason="missing_dimension_keys"`.
- The same planner now turns unsafe compiler-supplied dimension identifiers into blocked specs with `reason="unsafe_dimension_keys"` and keeps those identifiers out of `sql_text`.
- `ClickHouseRevenueRows.plan(...)` now treats explicit `compiler_runtime_plan` as authoritative: when spec building yields nothing executable, it returns a blocked `RevenueRowPlan` instead of dropping into the legacy SQL builder.
- The legacy accepted-graph SQL builder still runs for requests that do not carry `compiler_runtime_plan`.
