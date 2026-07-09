# Task 4: Analysis Assets Across Turns

## Status

Completed on branch `codex/production-multiturn-agent-runtime`.

## What changed

- Added `bi_agent/runtime/analysis_assets.py` to derive reusable analysis assets from the Answer Package:
  - `compiler_runtime_plan`
  - `contract_gap_diagnostic`
  - `claim_context_slot`
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

## Reviewer Fix Round 3

Wording correction for this report: the reusable claim asset in Task 4 is `claim_context_slot`. Earlier mentions of `verified_claim_slot` were stale wording from a prior design shape.

### RED

Pre-fix checks against `HEAD`:

```bash
tmp_old=/tmp/analysis_assets_head_task4_$$.py
cp <(git show HEAD:bi_agent/runtime/analysis_assets.py) "$tmp_old"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_old"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('analysis_assets_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assets = mod.build_analysis_assets({
    'run_id': 'run-old-claim-class',
    'sections': [{
        'section_id': 'summary',
        'payload': {'claim_groups': [
            {
                'text': '候选机制不该升级成业务真值。',
                'evidence_refs': ['event_evidence:inline'],
                'evidence_type': 'candidate_mechanism',
                'strength': 'high',
                'wording_limit': 'quantified',
                'verifier_status': 'passed',
            },
            {
                'text': '上下文证据不该升级成业务真值。',
                'evidence_refs': ['outlier_scan:inline'],
                'evidence_type': 'contextual_evidence',
                'strength': 'high',
                'wording_limit': 'supported',
                'verifier_status': 'passed',
            },
        ]}
    }],
})
for asset in assets:
    assert asset['can_support_business_truth'] is False, asset
PY
```

Observed failure:

```text
AssertionError: {'asset_type': 'claim_context_slot', 'status': 'claim_supported', 'source_run_id': 'run-old-claim-class', 'text': '候选机制不该升级成业务真值。', 'evidence_refs': ['event_evidence:inline'], 'strength': 'high', 'evidence_type': 'candidate_mechanism', 'limitations': [], 'verifier_status': 'passed', 'wording_limit': 'quantified', 'can_support_business_truth': True}
```

```bash
tmp_old=/tmp/analysis_assets_head_task4_$$.py
cp <(git show HEAD:bi_agent/runtime/analysis_assets.py) "$tmp_old"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_old"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('analysis_assets_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assets = mod.merge_analysis_assets(
    ({
        'asset_type': 'claim_context_slot',
        'status': 'context_only',
        'text': '直营渠道贡献较高。',
        'evidence_refs': ('segment_contribution:inline',),
        'target_metric': 'paid_amount',
        'scope': 'all_users',
        'time_window': '2026-07-01..2026-07-07',
        'verifier_status': 'failed',
        'can_support_business_truth': False,
        'source_run_id': 'run-01',
    },),
    ({
        'asset_type': 'claim_context_slot',
        'status': 'claim_supported',
        'text': '直营渠道贡献较高。',
        'evidence_refs': ('segment_contribution:inline',),
        'target_metric': 'paid_amount',
        'scope': 'all_users',
        'time_window': '2026-07-01..2026-07-07',
        'verifier_status': 'passed',
        'can_support_business_truth': True,
        'source_run_id': 'run-02',
    },),
)
assert len(assets) == 1 and assets[0]['source_run_id'] == 'run-02', assets
PY
```

Observed failure:

```text
AssertionError: ({'asset_type': 'claim_context_slot', 'status': 'context_only', 'text': '直营渠道贡献较高。', 'evidence_refs': ['segment_contribution:inline'], 'target_metric': 'paid_amount', 'scope': 'all_users', 'time_window': '2026-07-01..2026-07-07', 'verifier_status': 'failed', 'can_support_business_truth': False, 'source_run_id': 'run-01'}, {'asset_type': 'claim_context_slot', 'status': 'claim_supported', 'text': '直营渠道贡献较高。', 'evidence_refs': ['segment_contribution:inline'], 'target_metric': 'paid_amount', 'scope': 'all_users', 'time_window': '2026-07-01..2026-07-07', 'verifier_status': 'passed', 'can_support_business_truth': True, 'source_run_id': 'run-02'})
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase7.test_analysis_assets
python3 -m unittest tests.phase7.test_analysis_assets tests.phase7.test_conversation_runtime tests.phase7.test_conversation_persistence tests.phase4.test_workflow_artifacts_answer
```

Results:

```text
Ran 9 tests in 0.086s
OK

Ran 62 tests in 0.686s
OK
```

### Fix summary

- `claim_context_slot.can_support_business_truth` now also checks `evidence_type`, so `candidate_mechanism` and `contextual_evidence` stay context-only even when strength and wording look strong.
- Reusable asset dedupe identity now keys `dimension_scan` and `claim_context_slot` by stable reusable content, while the latest status, verifier result, and truth metadata remain on the retained asset payload.
- Added regression tests for context-only evidence classes and metadata-only asset changes collapsing to the latest reusable asset.

## Task 4 final mixed-evidence claim boundary

### RED

Commands:

```bash
tmp_old_answer=/tmp/task4_old_answer_package_$$.py
cp <(git show de3fce7f:bi_agent/runtime/answer_package.py) "$tmp_old_answer"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_old_answer"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('answer_package_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
package = mod.build_answer_package(
    run_id='task4-old-answer',
    draft_claims=[{
        'text': '第一条证据强，但第二条仍是上下文。',
        'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'],
        'scope': 'full_sample',
        'time_window': '2026-01-01..2026-06-30',
        'target_metric': 'paid_amount',
    }],
    evidence=[
        {
            'evidence_ref': 'driver_decomposition:inline',
            'evidence_type': 'accounting_contribution',
            'strength': 'high',
            'wording_limit': 'quantified',
            'limitations': [],
            'typed_payload': {},
        },
        {
            'evidence_ref': 'outlier_scan:inline',
            'evidence_type': 'contextual_evidence',
            'strength': 'medium',
            'wording_limit': 'contextual',
            'limitations': [],
            'typed_payload': {},
        },
    ],
    checkpoint_events=[],
    proposed_graph=[],
    accepted_graph=['driver_decomposition', 'outlier_scan', 'answer_verify'],
    rejected_or_degraded_mutations=[],
    validator_results=[],
    sql_text='SELECT 1',
    sql_hash='hash',
    artifact_audit={},
)
claim_group = package['sections'][0]['payload']['claim_groups'][0]
assert claim_group['evidence_types'] == ['accounting_contribution', 'contextual_evidence'], claim_group
PY

tmp_old_assets=/tmp/task4_old_analysis_assets_$$.py
cp <(git show de3fce7f:bi_agent/runtime/analysis_assets.py) "$tmp_old_assets"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_old_assets"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('analysis_assets_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assets = mod.build_analysis_assets({
    'run_id': 'task4-old-assets',
    'sections': [{
        'section_id': 'summary',
        'payload': {'claim_groups': [{
            'text': '第一条证据强，但第二条仍是上下文。',
            'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'],
            'evidence_type': 'accounting_contribution',
            'strength': 'high',
            'wording_limit': 'quantified',
            'verifier_status': 'passed',
        }]}
    }]
})
asset = next(item for item in assets if item['asset_type'] == 'claim_context_slot')
assert asset['can_support_business_truth'] is False, asset
PY
```

Observed failure:

```text
Traceback (most recent call last):
  File "<stdin>", line 44, in <module>
KeyError: 'evidence_types'

Traceback (most recent call last):
  File "<stdin>", line 22, in <module>
AssertionError: {'asset_type': 'claim_context_slot', 'status': 'claim_supported', 'source_run_id': 'task4-old-assets', 'text': '第一条证据强，但第二条仍是上下文。', 'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'], 'strength': 'high', 'evidence_type': 'accounting_contribution', 'limitations': [], 'verifier_status': 'passed', 'wording_limit': 'quantified', 'can_support_business_truth': True, 'target_metric': '', 'scope': '', 'time_window': ''}
```

### GREEN

Commands:

```bash
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY'
from bi_agent.runtime.answer_package import build_answer_package
package = build_answer_package(
    run_id='task4-new-answer',
    draft_claims=[{
        'text': '第一条证据强，但第二条仍是上下文。',
        'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'],
        'scope': 'full_sample',
        'time_window': '2026-01-01..2026-06-30',
        'target_metric': 'paid_amount',
    }],
    evidence=[
        {
            'evidence_ref': 'driver_decomposition:inline',
            'evidence_type': 'accounting_contribution',
            'strength': 'high',
            'wording_limit': 'quantified',
            'limitations': [],
            'typed_payload': {},
        },
        {
            'evidence_ref': 'outlier_scan:inline',
            'evidence_type': 'contextual_evidence',
            'strength': 'medium',
            'wording_limit': 'contextual',
            'limitations': [],
            'typed_payload': {},
        },
    ],
    checkpoint_events=[],
    proposed_graph=[],
    accepted_graph=['driver_decomposition', 'outlier_scan', 'answer_verify'],
    rejected_or_degraded_mutations=[],
    validator_results=[],
    sql_text='SELECT 1',
    sql_hash='hash',
    artifact_audit={},
)
claim_group = package['sections'][0]['payload']['claim_groups'][0]
assert claim_group['evidence_types'] == ['accounting_contribution', 'contextual_evidence'], claim_group
assert claim_group['wording_limits'] == ['quantified', 'contextual'], claim_group
PY

PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY'
from bi_agent.runtime.analysis_assets import build_analysis_assets
assets = build_analysis_assets({
    'run_id': 'task4-new-assets',
    'sections': [{
        'section_id': 'summary',
        'payload': {'claim_groups': [{
            'text': '第一条证据强，但第二条仍是上下文。',
            'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'],
            'evidence_type': 'accounting_contribution',
            'evidence_types': ['accounting_contribution', 'contextual_evidence'],
            'strength': 'high',
            'strengths': ['high', 'medium'],
            'wording_limit': 'quantified',
            'wording_limits': ['quantified', 'contextual'],
            'verifier_status': 'passed',
        }]}
    }]
})
asset = next(item for item in assets if item['asset_type'] == 'claim_context_slot')
assert asset['can_support_business_truth'] is False, asset
assert asset['status'] == 'context_only', asset
PY

python3 -m unittest tests.phase7.test_analysis_assets tests.phase4.test_workflow_artifacts_answer tests.phase5.test_answer_package_claim_groups
```

Results:

```text
Ran 41 tests in 0.540s
OK
```

### Fix summary

- `build_claim_groups(...)` now preserves per-ref `evidence_types`, `strengths`, and `wording_limits`, while retaining the old singular fields for existing readers.
- `claim_context_slot.can_support_business_truth` now evaluates the aggregated evidence boundary, so any contextual or candidate component keeps the slot in `context_only`.
- Added regressions for mixed-evidence claim groups and for analysis asset truth gating when the first ref is strong and a later ref is contextual.

## Reviewer Fix Round 5

### RED

Pre-fix checks against baseline commit `e3014667`:

```bash
tmp_answer=/tmp/task4_answer_old_$$.py
git show e3014667:bi_agent/runtime/answer_package.py > "$tmp_answer"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_answer"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('task4_answer_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
package = mod.build_answer_package(
    run_id='old-missing-meta',
    draft_claims=[{
        'text': '第二条证据缺元数据。',
        'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'],
        'scope': 'full_sample',
        'time_window': '2026-01-01..2026-06-30',
        'target_metric': 'paid_amount',
    }],
    evidence=[
        {
            'evidence_ref': 'driver_decomposition:inline',
            'evidence_type': 'accounting_contribution',
            'strength': 'high',
            'wording_limit': 'quantified',
            'limitations': [],
            'typed_payload': {},
        },
        {
            'evidence_ref': 'outlier_scan:inline',
            'strength': 'medium',
            'limitations': [],
            'typed_payload': {},
        },
    ],
    checkpoint_events=[],
    proposed_graph=[],
    accepted_graph=['driver_decomposition', 'outlier_scan', 'answer_verify'],
    rejected_or_degraded_mutations=[],
    validator_results=[],
    sql_text='SELECT 1',
    sql_hash='hash',
    artifact_audit={},
)
claim_group = package['sections'][0]['payload']['claim_groups'][0]
assert claim_group['evidence_types'] == ['accounting_contribution', 'missing'], claim_group
PY
```

Observed failure:

```text
AssertionError: {'text': '第二条证据缺元数据。', 'scope': 'full_sample', 'baseline': {}, 'target': {}, 'target_metric': 'paid_amount', 'time_window': '2026-01-01..2026-06-30', 'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'], 'evidence_type': 'accounting_contribution', 'evidence_types': ['accounting_contribution'], 'strength': 'high', 'strengths': ['high', 'medium'], 'wording_limit': 'quantified', 'wording_limits': ['quantified'], 'limitations': [], 'verifier_status': 'failed'}
```

```bash
tmp_assets=/tmp/task4_assets_old_$$.py
git show e3014667:bi_agent/runtime/analysis_assets.py > "$tmp_assets"
PYTHONPATH=/Users/luka/.codex/worktrees/250d/waje-bi-v2 python3 - <<'PY' "$tmp_assets"
import importlib.util
import sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('task4_assets_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assets = mod.build_analysis_assets(
    {
        'run_id': 'run-old-missing-meta',
        'sections': [{
            'section_id': 'summary',
            'payload': {'claim_groups': [{
                'text': '缺失元数据的证据组不能复用成业务真值。',
                'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'],
                'evidence_type': 'accounting_contribution',
                'evidence_types': ['accounting_contribution'],
                'strength': 'high',
                'strengths': ['high', 'medium'],
                'wording_limit': 'quantified',
                'wording_limits': ['quantified'],
                'verifier_status': 'passed',
            }]}
        }],
    }
)
claim = next(asset for asset in assets if asset['asset_type'] == 'claim_context_slot')
assert claim['can_support_business_truth'] is False, claim
PY
```

Observed failure:

```text
AssertionError: {'asset_type': 'claim_context_slot', 'status': 'claim_supported', 'source_run_id': 'run-old-missing-meta', 'text': '缺失元数据的证据组不能复用成业务真值。', 'evidence_refs': ['driver_decomposition:inline', 'outlier_scan:inline'], 'strength': 'high', 'strengths': ['high', 'medium'], 'evidence_type': 'accounting_contribution', 'evidence_types': ['accounting_contribution'], 'limitations': [], 'verifier_status': 'passed', 'wording_limit': 'quantified', 'wording_limits': ['quantified'], 'can_support_business_truth': True, 'target_metric': '', 'scope': '', 'time_window': ''}
```

```bash
tmp_root=$(mktemp -d /tmp/task4-pg-old-XXXXXX)
mkdir -p "$tmp_root/bi_agent/conversation" "$tmp_root/tools/runtime"
git show e3014667:bi_agent/conversation/postgres_store.py > "$tmp_root/bi_agent/conversation/postgres_store.py"
cp tools/runtime/conversation-runtime.sql "$tmp_root/tools/runtime/conversation-runtime.sql"
PYTHONPATH="/Users/luka/.codex/worktrees/250d/waje-bi-v2:$tmp_root" python3 - <<'PY' "$tmp_root"
import importlib.util
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
path = root / 'bi_agent' / 'conversation' / 'postgres_store.py'
spec = importlib.util.spec_from_file_location('task4_pg_old', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
class FakeCursor:
    def fetchone(self):
        return None
    def fetchall(self):
        return []
class FakeConnection:
    def __init__(self):
        self.statements = []
        self.commits = 0
    def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))
        return FakeCursor()
    def commit(self):
        self.commits += 1
store = mod.PostgresConversationStore(FakeConnection())
store.save_analysis_assets(
    'thread-pg',
    'topic-pg',
    [{'asset_type': 'compiler_runtime_plan', 'status': 'usable', 'payload': {'query_intents': ['dimension_scan_reuse']}}],
)
assert store.connection.commits == 1, store.connection.commits
PY
```

Observed failure:

```text
AssertionError: 3
```

### GREEN

Command:

```bash
python3 -m unittest tests.phase7.test_analysis_assets tests.phase7.test_conversation_persistence tests.phase4.test_workflow_artifacts_answer tests.phase5.test_answer_package_claim_groups
```

Result:

```text
Ran 51 tests in 0.549s

OK
```

### Fix summary

- `build_claim_groups(...)` now keeps one metadata slot per evidence ref and fills absent `evidence_type`, `strength`, or `wording_limit` with `missing`.
- `claim_context_slot` truth reuse now stops on `missing` or `unknown` metadata, together with the existing candidate and contextual boundaries.
- `save_analysis_assets(...)` in the Postgres store now commits once after delete, insert, and audit write, so the replacement path stays atomic on one connection transaction.
- Added focused regressions for missing mixed-evidence metadata and the single-commit Postgres save path.
