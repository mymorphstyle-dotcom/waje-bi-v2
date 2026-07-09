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
