# Task 2 Architecture Completion Report

## Scope

Task 2 extracts the deep revenue runtime plan compiler out of `compiler.py`, keeps Task 1 contract-gap descriptors intact inside `row_shapes`, and lets prior analysis assets influence the runtime plan shape.

Implemented files:

- `bi_agent/runtime/revenue_runtime_plan.py`
- `bi_agent/runtime/compiler.py`
- `bi_agent/runtime/langgraph_workflow.py`
- `tests/phase4/test_revenue_runtime_plan.py`
- `tests/phase4/test_recipe_registry_and_compiler.py`

## TDD Record

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_revenue_runtime_plan
```

Observed failure:

```text
ModuleNotFoundError: No module named 'bi_agent.runtime.revenue_runtime_plan'
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase4.test_revenue_runtime_plan
python3 -m unittest tests.phase4.test_recipe_registry_and_compiler
python3 -m unittest tests.phase4.test_workflow_artifacts_answer
python3 -m unittest tests.phase4.test_llm_workflow
```

Observed result:

```text
Ran 3 tests in 0.000s
OK

Ran 20 tests in 0.005s
OK

Ran 24 tests in 0.735s
OK

Ran 150 tests in 5.743s
OK
```

## What Changed

### 1. Added a dedicated revenue runtime plan builder

`bi_agent/runtime/revenue_runtime_plan.py` now owns the revenue-specific compiler output assembly:

- target window compilation
- baseline compilation
- dimension candidate selection
- capability parameter normalization
- query intent selection
- row-shape construction
- contract-gap descriptor projection
- prior asset reference reuse

The plan now exposes:

- `target_metric`
- `diagnostic_axes`
- `windows`
- `baselines`
- `dimension_candidates`
- `measures`
- `capability_params`
- `query_intents`
- `row_shapes`
- `contract_gaps`
- `asset_inputs_used`

### 2. Preserved explicit contract-gap descriptors from Task 1

The builder keeps descriptor dictionaries in `row_shapes[*]["contract_gaps"]` instead of flattening back to strings.

Current preserved descriptors:

- `high_value_user_contract_missing`
- `gameplay_contract_missing`
- `event_context_contract_missing`
- `payment_status_contract_missing`
- `duplicate_order_contract_missing`

This keeps downstream diagnostics and answer-package audit logic on the same structured contract surface added in Task 1.

### 3. Simplified `compiler.py` into graph acceptance plus plan delegation

`bi_agent/runtime/compiler.py` now:

- accepts `prior_analysis_assets`
- delegates plan construction to `build_revenue_runtime_plan(...)`
- drops the embedded revenue runtime-plan assembly code

The graph compiler still owns:

- question-family gating
- capability support checks
- diagnostic-axis expansion
- mutation records

The revenue plan builder owns the runtime-plan shape.

### 4. Wired prior analysis assets through workflow entry

`bi_agent/runtime/langgraph_workflow.py` now passes:

```python
prior_analysis_assets=tuple(state["request"].get("prior_analysis_assets") or ())
```

into `compile_graph(...)`.

That gives the compiler/runtime-plan layer a real request-side input for reusable assets such as prior dimension scans.

### 5. Added direct plan tests and compiler integration coverage

`tests/phase4/test_revenue_runtime_plan.py` covers:

- multi-baseline window/baseline/param compilation
- factor-topk dimension candidate compilation
- prior-asset reuse turning into `asset_inputs_used` and `dimension_scan_reuse`

`tests/phase4/test_recipe_registry_and_compiler.py` now also verifies:

- compiler output uses `baselines`
- compiler forwards prior analysis assets into the runtime plan

## Constraint Check

- No new dependency added
- No local business-answer template added
- No keyword shortcuts were introduced for business judgment beyond existing diagnostic-axis routing already in compiler scope
- Contract-gap hard boundaries remain deterministic and explicit
- Task 1 structured descriptors remain intact
- LLM retry behavior unchanged
- `artifacts/` behavior unchanged

## Concerns

- This task adds compiler/workflow support for `prior_analysis_assets` when the request already carries them. Cross-turn extraction, persistence, and manifest population still belong to the later analysis-assets task.

## Reviewer Follow-up TDD

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_revenue_runtime_plan tests.phase4.test_recipe_registry_and_compiler tests.phase7.test_conversation_runtime tests.phase7.test_agent_core_bridge
```

Observed failure:

```text
ERROR: test_bound_context_takes_precedence_over_day_over_day_text
TypeError: build_revenue_runtime_plan() got an unexpected keyword argument 'bound_context'

ERROR: test_compiler_uses_bound_runtime_context_before_question_text_fallback
TypeError: compile_graph() got an unexpected keyword argument 'bound_context'

ERROR: test_runtime_carries_prior_assets_into_run_request
TypeError: handle_message() got an unexpected keyword argument 'prior_analysis_assets'

ERROR: test_agent_core_passes_prior_assets_to_workflow_request
TypeError: run_message() got an unexpected keyword argument 'prior_analysis_assets'

FAIL: test_candidate_only_dimensions_require_contract_gap_descriptors
AssertionError: 'package_name' not found in set()
```

### GREEN

Command:

```bash
python3 -m unittest tests.phase4.test_revenue_runtime_plan tests.phase4.test_recipe_registry_and_compiler tests.phase7.test_conversation_runtime tests.phase7.test_agent_core_bridge
```

Observed result:

```text
Ran 79 tests in 0.561s

OK
```

## Re-review Important Findings Fix

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_revenue_runtime_plan tests.phase7.test_agent_core_bridge
```

Observed failure:

```text
FAIL: test_non_matching_prior_assets_do_not_suppress_needed_scan
AssertionError: Tuples differ: ('query:region-scan', 'query:channel-contribution') != ()

FAIL: test_prior_assets_reduce_repeated_scans
AssertionError: 'dimension_scan' unexpectedly found in ('daily_metric_baselines', 'dimension_scan_reuse', 'dimension_scan')

ERROR: test_main_accepts_prior_analysis_assets_argument
SystemExit: 2
python3 -m unittest: error: unrecognized arguments: --prior-analysis-assets [...]
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase4.test_revenue_runtime_plan tests.phase7.test_agent_core_bridge
python3 -m unittest tests.phase4.test_revenue_runtime_plan tests.phase7.test_agent_core_bridge tests.phase4.test_recipe_registry_and_compiler tests.phase7.test_conversation_runtime
```

Observed result:

```text
Ran 38 tests in 0.355s
OK

Ran 81 tests in 0.565s
OK
```

### Fix Summary

- Tightened prior asset reuse in `bi_agent/runtime/revenue_runtime_plan.py` so reuse only counts usable `dimension_scan` assets whose dimension covers the required scan surface for the current runtime plan.
- Suppressed duplicate `dimension_scan` when reusable prior assets already cover the required dimensions; non-matching assets now leave the scan in place and are excluded from `asset_inputs_used`.
- Added `--prior-analysis-assets` support in `bi_agent/conversation/agent_core.py` so the executable CLI path can pass serialized prior assets into `run_message(...)`.
