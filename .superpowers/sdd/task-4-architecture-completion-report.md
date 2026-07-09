# Task 4: Analysis Assets Across Turns

## Status

Completed on branch `codex/production-multiturn-agent-runtime`.

## What changed

- Added `bi_agent/runtime/analysis_assets.py` to derive reusable analysis assets from the Answer Package:
  - `compiler_runtime_plan`
  - `contract_gap_diagnostic`
  - `verified_claim_slot`
- Extended `ContextManifest` with `analysis_assets` so later turns keep topic-level asset context in persisted manifests.
- Added topic-level `save_analysis_assets(...)` / `list_analysis_assets(...)` support to:
  - `InMemoryConversationStore`
  - `PostgresConversationStore`
- Added `waje_runtime.analysis_assets` to `tools/runtime/conversation-runtime.sql`.
- Updated `ConversationRuntime` to:
  - load topic assets for the active topic
  - include them in `context_manifest.analysis_assets`
  - merge them into `ConversationRunRequest.prior_analysis_assets`
- Updated `ConversationAgentCore` to persist built assets after a successful Answer Package is recorded.

## Tests

TDD path:

1. Added `tests/phase7/test_analysis_assets.py`.
2. Verified red state with:

```bash
python3 -m unittest tests.phase7.test_analysis_assets
```

Initial failure:

```text
ModuleNotFoundError: No module named 'bi_agent.runtime.analysis_assets'
```

3. Implemented the minimal runtime/store changes.
4. Verified green with:

```bash
python3 -m unittest tests.phase7.test_analysis_assets
python3 -m unittest tests.phase7.test_conversation_runtime tests.phase7.test_agent_core_bridge tests.phase7.test_conversation_persistence
python3 -m unittest discover -s tests/phase7 -p 'test_*.py'
```

## Notes

- Topic assets are appended per successful answer package. There is no cross-turn compaction yet; the runtime dedupes per request before handing assets back into the compiler path.
- No gateway-specific template logic was added. The change stays in runtime/store metadata and Answer Package-derived assets.

## Reviewer Fix Round

### RED

Command:

```bash
python3 -m unittest tests.phase7.test_analysis_assets tests.phase7.test_conversation_runtime tests.phase7.test_conversation_persistence tests.phase4.test_revenue_runtime_plan tests.phase4.test_recipe_registry_and_compiler
```

Observed failure during the reviewer-fix pass:

```text
FAIL: test_runtime_carries_prior_assets_into_run_request (tests.phase7.test_conversation_runtime.ConversationRuntimeTest)
AssertionError: normalized dimension_scan assets now include dimensions=['channel']
```

This exposed the remaining contract mismatch after adding bounded asset normalization.

### GREEN

Command:

```bash
python3 -m unittest tests.phase7.test_analysis_assets tests.phase7.test_conversation_runtime tests.phase7.test_conversation_persistence tests.phase4.test_revenue_runtime_plan tests.phase4.test_recipe_registry_and_compiler
```

Result:

```text
Ran 62 tests in 0.224s

OK
```

### Fix summary

- Answer Package admin audit now carries `row_query_plan` metadata so the asset builder can emit real `dimension_scan` assets with `query_ref`.
- `ConversationAgentCore` now preserves the merged `prior_analysis_assets` from `ConversationRuntime` instead of overwriting them with only external request assets.
- Topic analysis assets are now normalized, deduped, and capped at 20 items in both in-memory and Postgres stores.
- Claim assets were downgraded from `verified_claim_slot` to `claim_context_slot`, and now preserve `verifier_status`, `strength`, `limitations`, and `evidence_refs` without overstating business-truth usability.
- `context_manifest.analysis_assets` now records the same merged asset set that flows into compiler input, including externally supplied prior assets.
