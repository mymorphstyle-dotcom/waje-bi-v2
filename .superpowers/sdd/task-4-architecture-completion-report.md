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

## Reviewer Fix Round 2

### RED

Pre-fix contract checks against `HEAD`:

```bash
tmp_old=/tmp/analysis_assets_old_claim_$$.py
git show HEAD:bi_agent/runtime/analysis_assets.py > "$tmp_old"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_old"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('analysis_assets_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assets = mod.build_analysis_assets({
    'run_id': 'run-old',
    'sections': [{
        'section_id': 'summary',
        'payload': {'claim_groups': [{
            'text': '候选判断',
            'evidence_refs': ['segment_contribution:inline'],
            'strength': 'high',
            'wording_limit': 'candidate',
            'verifier_status': 'passed',
        }]}
    }],
})
claim = next(asset for asset in assets if asset['asset_type'] == 'claim_context_slot')
assert claim['can_support_business_truth'] is False, claim
PY
```

Observed failure:

```text
AssertionError: {'asset_type': 'claim_context_slot', 'status': 'claim_supported', 'source_run_id': 'run-old', 'text': '候选判断', 'evidence_refs': ['segment_contribution:inline'], 'strength': 'high', 'evidence_type': '', 'limitations': [], 'verifier_status': 'passed', 'wording_limit': 'candidate', 'can_support_business_truth': True}
```

```bash
tmp_old=/tmp/analysis_assets_old_dedupe_$$.py
git show HEAD:bi_agent/runtime/analysis_assets.py > "$tmp_old"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_old"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('analysis_assets_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assets = mod.merge_analysis_assets(
    ({'asset_type': 'dimension_scan', 'status': 'usable', 'dimensions': ('channel',), 'query_ref': 'query:channel-scan', 'source_run_id': 'run-01'},),
    ({'asset_type': 'dimension_scan', 'status': 'usable', 'dimensions': ('channel',), 'query_ref': 'query:channel-scan', 'source_run_id': 'run-02'},),
)
assert len(assets) == 1 and assets[0]['source_run_id'] == 'run-02', assets
PY
```

Observed failure:

```text
AssertionError: ({'asset_type': 'dimension_scan', 'status': 'usable', 'dimensions': ['channel'], 'query_ref': 'query:channel-scan', 'source_run_id': 'run-01', 'dimension': 'channel'}, {'asset_type': 'dimension_scan', 'status': 'usable', 'dimensions': ['channel'], 'query_ref': 'query:channel-scan', 'source_run_id': 'run-02', 'dimension': 'channel'})
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase7.test_analysis_assets
python3 -m unittest tests.phase4.test_workflow_artifacts_answer
python3 -m unittest tests.phase7.test_analysis_assets tests.phase7.test_conversation_runtime tests.phase7.test_conversation_persistence tests.phase4.test_workflow_artifacts_answer
```

Results:

```text
Ran 8 tests in 0.084s
OK

Ran 24 tests in 0.473s
OK

Ran 61 tests in 0.707s
OK
```

### Fix summary

- `claim_context_slot.can_support_business_truth` now requires a reusable wording limit and preserves the original `wording_limit` on the stored asset.
- Reusable asset dedupe now ignores `source_run_id`, so identical semantic content collapses to one asset while the latest run metadata stays on the retained payload.
- Phase 7 tests now lock the reviewer findings: claim assets keep wording/limitations/truth boundary, and repeated reusable content keeps only the latest asset.
