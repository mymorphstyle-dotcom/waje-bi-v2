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
