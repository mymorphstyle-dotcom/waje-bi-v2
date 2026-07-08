# Task 1 Report: ConversationAgentCore Live Conversation Harness

## Scope

- Added live conversation case `q2_q1_wajespecial_long_followup` to `evals/phase7/conversation_scenarios.yaml`.
- Added harness entrypoint `tools/phase7/run_live_conversation_system_test.py`.
- Extended `ConversationAgentCore` with `from_environment(...)`, optional `run_id`, and harness-facing return fields.
- Added package markers `tools/__init__.py` and `tools/phase7/__init__.py` so the unittest import path resolves.
- Added schema test coverage in `tests/phase7/test_agent_core_bridge.py`.

## TDD Record

### Red

Command:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge -k live_conversation_case_schema_supports_clarification_resume
```

First output after wiring the test into `unittest` discovery:

```text
E
======================================================================
ERROR: test_live_conversation_case_schema_supports_clarification_resume (tests.phase7.test_agent_core_bridge.AgentCoreBridgeTest)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/luka/.codex/worktrees/250d/waje-bi-v2/tests/phase7/test_agent_core_bridge.py", line 89, in test_live_conversation_case_schema_supports_clarification_resume
    from tools.phase7.run_live_conversation_system_test import load_cases
ModuleNotFoundError: No module named 'tools.phase7.run_live_conversation_system_test'

----------------------------------------------------------------------
Ran 1 test in 0.003s

FAILED (errors=1)
```

Note: the very first run with the brief’s literal top-level function shape produced `Ran 0 tests in 0.000s`. This repo’s file uses `unittest.TestCase`, so I moved the assertion into the existing test class to get a real red signal.

### Green

Targeted command:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge -k live_conversation_case_schema_supports_clarification_resume
```

Output:

```text
.
----------------------------------------------------------------------
Ran 1 test in 0.080s

OK
```

Regression check:

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge
```

Output:

```text
....
----------------------------------------------------------------------
Ran 4 tests in 0.075s

OK
```

## Implementation Notes

### Case loader and harness

- `load_cases(...)` reads the YAML and keeps only entries with `id`.
- `select_cases(...)` filters by `--case`.
- `run_case(...)` executes the case through `ConversationAgentCore.run_message(...)`, records per-turn fields, and resumes one turn if the runtime returns `waiting_for_clarification` and the case provides `clarification_response`.
- Dry-run mode works with no real runtime env and writes local artifacts under the requested `artifact-dir`.

### ConversationAgentCore changes

- `ConversationAgentCore.from_environment(real_llm=False, real_clickhouse=False)`:
  - real mode: builds `PostgresConversationStore.from_env()` and uses env-backed conversation LLM only when `real_llm=True`
  - dry-run mode: uses `InMemoryConversationStore` plus a tiny `_dry_run_workflow(...)`
- `run_message(...)` now accepts the brief’s public shape:
  - `thread_id`
  - `user_message`
  - optional `user_id`
  - optional `permission_context`
- Existing callers that pass `run_id` and `role` still work.
- Returned dict now includes `topic_id`, `context_manifest`, `answer_package`, `accepted_graph`, `llm_calls`, and `quality_review` when available.

## Harness Verification

### Dry-run harness

Command:

```bash
python3 tools/phase7/run_live_conversation_system_test.py --case q2_q1_wajespecial_long_followup --artifact-dir artifacts/phase7/live-conversation
```

Output:

```json
{"case_count": 1, "case_ids": ["q2_q1_wajespecial_long_followup"]}
```

Artifact written:

- `artifacts/phase7/live-conversation/q2_q1_wajespecial_long_followup.json`

### Real smoke case

Command:

```bash
python3 tools/phase7/run_live_conversation_system_test.py --case q2_q1_wajespecial_long_followup --real-llm --real-clickhouse --artifact-dir artifacts/phase7/live-conversation
```

Observed output:

```text
Traceback (most recent call last):
  File "/Users/luka/.codex/worktrees/250d/waje-bi-v2/tools/phase7/run_live_conversation_system_test.py", line 98, in <module>
    main()
  File "/Users/luka/.codex/worktrees/250d/waje-bi-v2/tools/phase7/run_live_conversation_system_test.py", line 83, in main
    core = ConversationAgentCore.from_environment(
  File "/Users/luka/.codex/worktrees/250d/waje-bi-v2/bi_agent/conversation/agent_core.py", line 180, in from_environment
    PostgresConversationStore.from_env(),
  File "/Users/luka/.codex/worktrees/250d/waje-bi-v2/bi_agent/conversation/postgres_store.py", line 33, in from_env
    raise RuntimeError("WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required")
RuntimeError: WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required
```

## Missing Runtime Inputs

Blocked item:

| Item | Why it is needed | Observed state | Owner |
| --- | --- | --- | --- |
| `WAJE_RUNTIME_DATABASE_URL` or `DATABASE_URL` | real conversation thread/run persistence via `PostgresConversationStore.from_env()` | missing in current shell; real smoke stopped here | local runtime/deployment owner |

Not verified in this shell because the real run stopped before LLM/runtime binding:

| Item | Why it is likely needed next | Owner |
| --- | --- | --- |
| `WAJE_LLM_MODEL` | required by `OpenAICompatibleLLMClient.from_env()` when `--real-llm` is set | LLM runtime owner |
| `WAJE_LLM_API_KEY` or `OPENAI_API_KEY` or `DEEPSEEK_API_KEY` | required by `OpenAICompatibleLLMClient.from_env()` when `--real-llm` is set | LLM runtime owner |
| `WAJE_CLICKHOUSE_*` | likely required downstream once the workflow touches real ClickHouse-bound capabilities | data/runtime owner |

## Files Changed

- `bi_agent/conversation/agent_core.py`
- `evals/phase7/conversation_scenarios.yaml`
- `tests/phase7/test_agent_core_bridge.py`
- `tools/__init__.py`
- `tools/phase7/__init__.py`
- `tools/phase7/run_live_conversation_system_test.py`

## Commit

Planned brief message:

```text
test: add live conversation agent core harness
```
