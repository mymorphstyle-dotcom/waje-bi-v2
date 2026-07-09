# Task 6 Architecture Completion Report

## What changed

- Adjusted `tools/phase7/run_live_conversation_system_test.py` so strict eval only fails on hard boundaries:
  - missing required capabilities
  - missing explicitly marked hard-boundary wording
  - claim support policy failure
  - `quality_gate.blocks_display = true`
- Kept `missing_final_answer_text` as semantic warning output in `expectation_review`; it no longer flips the turn to failed by itself.
- Added explicit `hard_boundary_final_answer_contains` support in `evals/phase7/conversation_scenarios.yaml` and moved the causal warning anchor `不能直接说` there for the two WajeSpecial follow-up cases.
- Added bridge tests for warning-only final wording anchors and semantically valid paraphrase.

## TDD RED/GREEN evidence

### RED

Added the failing test first in `tests/phase7/test_agent_core_bridge.py`, then ran:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge.AgentCoreBridgeTest.test_strict_eval_treats_final_wording_anchor_as_warning
```

Observed expected failure:

```text
FAIL: test_strict_eval_treats_final_wording_anchor_as_warning
AssertionError: True is not false
```

### GREEN

After implementation, reran:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge.AgentCoreBridgeTest.test_strict_eval_treats_final_wording_anchor_as_warning
python3 -m unittest tests.phase7.test_agent_core_bridge
python3 -m unittest discover -s tests/phase4 -p 'test_*.py'
python3 -m unittest discover -s tests/phase7 -p 'test_*.py'
python3 -m unittest discover -s tests/phase8 -p 'test_*.py'
```

All passed.

## Live eval command and result

Ran the required command:

```bash
python3 tools/phase7/run_live_conversation_system_test.py --case paid_amount_revenue_diagnostics_8_question_set --real-llm --real-clickhouse --strict-quality --artifact-dir artifacts/phase7/live-conversation-real-clickhouse-architecture-completion
```

Result:

- Command reached `ConversationAgentCore` / runtime path as required.
- Run failed in the real Postgres store before artifact write with:

```text
psycopg.errors.UndefinedTable: relation "waje_runtime.analysis_assets" does not exist
```

- No env-var gap was detected from this failure path.
- Artifact directory was not created because the runtime raised before the harness fallback could serialize a blocked artifact.
- Owner: local runtime / database schema owner.

## Files changed

- `tools/phase7/run_live_conversation_system_test.py`
- `evals/phase7/conversation_scenarios.yaml`
- `tests/phase7/test_agent_core_bridge.py`

## Self-review

- The failure split now lives in shared harness semantics, not in one case-specific branch.
- Warning-only wording anchors remain visible in `expectation_review`, so the live artifact still shows semantic drift when it happens.
- Explicit hard-boundary wording stays opt-in per case through scenario data, which keeps legal/permission boundaries configurable without adding local keyword heuristics.
- The live eval blocker is outside this task's owned files. It points to local Postgres schema drift and needs schema bootstrap before the live run can complete.

## Commits

- Implementation commit: `bc5a113fd154f6c73d7df2f25066825a91ce4fa0`
- Cleanup commit: `b2677474` removed the accidentally tracked SDD report from git.

---

## 2026-07-09 Live eval hard-failure remediation

### Root causes fixed

- `bi_agent/runtime/langgraph_workflow.py`
  - `business_intent` normalization assumed `baseline_candidates` was always iterable; real LLM output can return `null`, which raised `TypeError: 'NoneType' object is not iterable` before accepted graph / answer package generation.
  - `pattern_family` normalization let null-like or unsupported values pass through. Real LLM output used `none`, which later surfaced as `unsupported pattern_family: none`.
- `bi_agent/runtime/clickhouse_revenue_rows.py`
  - Query spec preference treated `event_evidence` as higher priority than executable compare-path queries, so a blocked `event_context_probe` could suppress an executable `daily_metric_baselines` query even when compare / driver nodes were accepted.

### TDD RED

Added failing tests first:

- `tests.phase4.test_llm_workflow.LLMWorkflowTest.test_business_intent_treats_null_list_fields_as_empty_lists`
- `tests.phase4.test_llm_workflow.LLMWorkflowTest.test_business_intent_normalizes_none_pattern_family_to_custom_baseline_for_period_recompare`
- `tests.phase4.test_llm_workflow.LLMWorkflowTest.test_business_intent_normalizes_unsupported_pattern_family_to_intra_period`
- `tests.phase4.test_clickhouse_revenue_rows.ClickHouseRevenueRowsTest.test_plan_prefers_executable_baseline_query_when_event_probe_is_blocked`

RED commands and observed failures:

```bash
python3 -m unittest tests.phase4.test_llm_workflow
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows
```

Observed RED signals:

- `TypeError: 'NoneType' object is not iterable`
- `AssertionError: 'none' != 'custom_baseline'`
- `AssertionError: 'surprise_mode' != 'intra_period'`
- baseline query selection returned empty SQL for blocked `event_context_probe`

### GREEN

Implemented minimal runtime hardening:

- treat nullable `baseline_candidates` as empty list during intent normalization
- normalize unsupported / null-like `pattern_family` values to supported families using existing business context:
  - custom baseline comparison context -> `custom_baseline`
  - weekday/weekend context -> `weekly`
  - otherwise -> `intra_period`
- prefer `daily_metric_baselines` before `event_context_probe` when `event_evidence` appears together with compare / driver capabilities

GREEN commands and results:

```bash
python3 -m unittest tests.phase4.test_llm_workflow
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows
python3 -m unittest tests.phase4.test_workflow_artifacts_answer
python3 -m unittest tests.phase7.test_agent_core_bridge
```

All passed.

### Live eval command and result

Ran:

```bash
python3 tools/phase7/run_live_conversation_system_test.py --case paid_amount_revenue_diagnostics_8_question_set --real-llm --real-clickhouse --strict-quality --artifact-dir artifacts/phase7/live-conversation-real-clickhouse-architecture-completion
```

Result:

- process exited non-zero under `--strict-quality`
- artifact written successfully
- all 8 turns completed
- original hard failures did not recur:
  - turn 2 no longer fails on `NoneType`
  - turn 3 no longer gets stuck on blocked `event_context_probe` when executable baseline query is available
  - turn 7 no longer fails on `unsupported pattern_family: none`

Remaining exact failures from artifact:

- top-level `status: failed`
- `strict_quality_failed: true`
- `quality_review.verified_claim_preserved: false`
- `answer_package.quality_gate.issues: ["missing_verified_claim"]`
- `real_clickhouse_review.real_clickhouse_verified: false`
- `real_clickhouse_review.issues: ["missing_clickhouse_result_refs", "missing_clickhouse_runtime_validator"]`

Artifact path:

- `artifacts/phase7/live-conversation-real-clickhouse-architecture-completion/paid_amount_revenue_diagnostics_8_question_set.json`

### Files changed

- `bi_agent/runtime/langgraph_workflow.py`
- `bi_agent/runtime/clickhouse_revenue_rows.py`
- `tests/phase4/test_llm_workflow.py`
- `tests/phase4/test_clickhouse_revenue_rows.py`

### Commit SHA

- `2591e98e`

---

## 2026-07-09 Remaining live eval remediation

### Root cause evidence

- `bi_agent/runtime/clickhouse_revenue_rows.py`
  - `_select_query_spec` hit the first preferred intent even when that spec was blocked and non-executable. For Task 6 turn 3 this let `dimension_scan_reuse` win with empty SQL, so the run lost current-turn `clickhouse_runtime` evidence and result refs.
- `bi_agent/runtime/langgraph_workflow.py`
  - terminal blocked explanations only auto-generated audit evidence for coverage-blocked cases. Contract-gap / validator-blocked exits could finish with no traceable blocked-boundary claim, which left claim-support verification without auditable refs.

### TDD RED/GREEN evidence

#### RED

Added failing tests first:

- `tests.phase4.test_clickhouse_revenue_rows.ClickHouseRevenueRowsTest.test_plan_prefers_executable_dimension_scan_when_reuse_is_blocked`
- `tests.phase4.test_workflow_artifacts_answer.WorkflowArtifactsAnswerTest.test_blocked_contract_gap_emits_auditable_evidence_and_claim`

RED commands:

```bash
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows.ClickHouseRevenueRowsTest.test_plan_prefers_executable_dimension_scan_when_reuse_is_blocked
python3 -m unittest tests.phase4.test_workflow_artifacts_answer.WorkflowArtifactsAnswerTest.test_blocked_contract_gap_emits_auditable_evidence_and_claim
```

Observed failures:

- `AssertionError: 'SELECT' not found in ''`
- `AssertionError: [] is not true`

#### GREEN

Minimal runtime fixes:

- skip blocked preferred query specs when executable alternatives exist; if any executable spec remains, choose that before falling back to blocked specs
- add blocked-boundary audit evidence/claim generation for contract-gap and validator exits
- include `scope` / `time_window` in boundary evidence payloads so answer-package verification can pass on terminal explanations

GREEN commands:

```bash
python3 -m unittest tests.phase4.test_clickhouse_revenue_rows
python3 -m unittest tests.phase4.test_workflow_artifacts_answer
python3 -m unittest tests.phase4.test_llm_workflow
python3 -m unittest tests.phase7.test_agent_core_bridge
```

Results:

- `tests.phase4.test_clickhouse_revenue_rows`: 13 passed
- `tests.phase4.test_workflow_artifacts_answer`: 25 passed
- `tests.phase4.test_llm_workflow`: 154 passed
- `tests.phase7.test_agent_core_bridge`: 34 passed

### Live eval result and artifact

Ran:

```bash
python3 tools/phase7/run_live_conversation_system_test.py --case paid_amount_revenue_diagnostics_8_question_set --real-llm --real-clickhouse --strict-quality --artifact-dir artifacts/phase7/live-conversation-real-clickhouse-architecture-completion
```

Result:

- top-level artifact `status: passed`
- `strict_quality_failed: false`
- all 8 turns completed
- turn 3 passed
- turn 3 `row_query_plan` is executable and now carries current-turn query refs:
  - `query_id: run-3f10291e7251:dimension_scan`
  - `query_hash/result_refs: 80146f9c559e323f74cdba3d398b0f5818f856af36282dc327f5f4ec829f5c47`
- turn 3 `clickhouse_runtime` validator is present and `ok=true`
- turn 3 `claim_support_policy_passed: true`
- turn 3 answer-package verifier `status: passed`

Artifact path:

- `artifacts/phase7/live-conversation-real-clickhouse-architecture-completion/paid_amount_revenue_diagnostics_8_question_set.json`

### Files changed

- `bi_agent/runtime/clickhouse_revenue_rows.py`
- `bi_agent/runtime/langgraph_workflow.py`
- `tests/phase4/test_clickhouse_revenue_rows.py`
- `tests/phase4/test_workflow_artifacts_answer.py`

### Commit SHA

- `9b2f1695`
