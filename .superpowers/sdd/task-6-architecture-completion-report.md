# Task 6 Architecture Completion Report

## What changed

- Adjusted `tools/phase7/run_live_conversation_system_test.py` so strict eval only fails on hard boundaries:
  - missing required capabilities
  - missing explicitly marked hard-boundary wording
  - claim support policy failure
  - `quality_gate.blocks_display = true`
- Kept `missing_final_answer_text` as semantic warning output in `expectation_review`; it no longer flips the turn to failed by itself.
- Added explicit `hard_boundary_final_answer_contains` support in `evals/phase7/conversation_scenarios.yaml` and moved the causal warning anchor `不能直接说` there for the two WajeSpecial follow-up cases.
- Added bridge tests that cover:
  - strict mode treating wording-anchor drift as warning-only
  - expectation review staying green when paraphrase keeps the business meaning and evidence chain intact

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
- The live eval blocker is outside this task’s owned files. I did not patch around the missing runtime table.

## Commit SHA

- Implementation commit: `bc5a113fd154f6c73d7df2f25066825a91ce4fa0`
