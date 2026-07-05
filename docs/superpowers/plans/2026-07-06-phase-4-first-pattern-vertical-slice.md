# Phase 4 First Pattern Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first executable, generalized `pattern_explanation` vertical slice for WAJE BI v2, with month-start as a regression case and sibling pattern cases covered by evals.

**Architecture:** Add a Python BI Agent Core inside this repo for Phase 4 runtime work. Python owns LangGraph execution, ClickHouse inspection/query access, capability runtime, evidence/result refs, artifact persistence, draft Answer Package, and verifier checks. Existing Ruby tools stay as Phase 1-3 contract-only validators and must keep passing.

**Tech Stack:** Python 3, LangGraph, ClickHouse read-only access through environment variables, YAML contracts, JSON artifacts under `artifacts/phase-4/`, existing Ruby contract validators, existing Next/TypeScript frontend/gateway left untouched.

---

## File Structure

- Create `requirements.txt`: Python runtime dependencies `langgraph`, `clickhouse-connect`, and `PyYAML`.
- Create `bi_agent/__init__.py`: package marker.
- Create `bi_agent/runtime/models.py`: dataclasses and enums for runs, roles, graph nodes, evidence, result refs, validator results, claims, artifacts.
- Create `bi_agent/runtime/contracts.py`: YAML contract loader with support/capability/metric lookup.
- Create `bi_agent/runtime/recipe_registry.py`: eight recipe entries and executable subgraph skeletons.
- Create `bi_agent/runtime/compiler.py`: proposed graph acceptance, degradation, rejection, targeted repair records, required evidence path enforcement.
- Create `bi_agent/runtime/sql_safety.py`: SELECT-only ClickHouse SQL validator.
- Create `bi_agent/runtime/clickhouse_runtime.py`: environment-driven ClickHouse client, schema inspection, binding candidates, bounded aggregate execution.
- Create `bi_agent/runtime/artifacts.py`: local JSON artifact writer and role visibility filter.
- Create `bi_agent/runtime/wording.py`: versioned wording policy loader and warning-only wording checks.
- Create `bi_agent/runtime/answer_package.py`: draft Answer Package builder and verifier.
- Create `bi_agent/runtime/langgraph_workflow.py`: LangGraph workflow wrapper with checkpoint events and honest failure behavior.
- Create `bi_agent/capabilities/data_quality_check.py`: coverage, duplicate, status, and month completeness checks.
- Create `bi_agent/capabilities/pattern_scan.py`: generalized pattern scanner for intra-period, weekly, event-relative, rolling, lag/recovery, and custom-baseline cases.
- Create `bi_agent/capabilities/formula_decompose.py`: current-data-covered formula decomposition attempt and degraded result behavior.
- Create `bi_agent/capabilities/event_evidence.py`: payday and reviewed-event candidate mechanism evidence.
- Create `bi_agent/capabilities/segment_bridge.py`: low-order segment bridge skeleton with permission and sparse-cell guards.
- Create `bi_agent/capabilities/joint_attribution.py`: escalation skeleton used when segment bridge fit is insufficient.
- Create `bi_agent/capabilities/outlier_scan.py`: exception and outlier scan over aggregated pattern windows.
- Create `tools/phase4/run_phase4_pattern_slice.py`: CLI entry point for real run and fixture run.
- Create `tools/phase4/validate_phase4.py`: Phase 4 validation runner.
- Create `evals/phase4/pattern_cases.yaml`: month-start regression plus weekly, event-relative, rolling, custom-baseline cases.
- Create `tests/phase4/*.py`: small `unittest` coverage for compiler, SQL safety, pattern math, workflow failure, artifact visibility, verifier, and eval harness.
- Create `docs/phase-4-closeout-status.md`: final closeout doc after implementation and validation.
- Modify `.gitignore`: ignore `artifacts/phase-4/`.

## Commit Plan

1. `docs: add phase 4 implementation plan`
2. `feat: add phase 4 python runtime contracts and registry`
3. `feat: add clickhouse validators and pattern capabilities`
4. `feat: wire langgraph workflow and answer artifacts`
5. `test: add phase 4 eval harness`
6. `docs: close out phase 4 vertical slice`

## Task 1: Plan Commit

**Files:**
- Create: `docs/superpowers/plans/2026-07-06-phase-4-first-pattern-vertical-slice.md`

- [ ] **Step 1: Verify plan exists**

Run:

```bash
test -f docs/superpowers/plans/2026-07-06-phase-4-first-pattern-vertical-slice.md
```

Expected: exit code `0`.

- [ ] **Step 2: Commit plan**

Run:

```bash
git add docs/superpowers/plans/2026-07-06-phase-4-first-pattern-vertical-slice.md
git commit -m "docs: add phase 4 implementation plan"
```

Expected: one commit with only the plan file.

## Task 2: Python Runtime Foundation, Contracts, Recipes, Compiler

**Files:**
- Create: `requirements.txt`
- Create: `bi_agent/__init__.py`
- Create: `bi_agent/runtime/models.py`
- Create: `bi_agent/runtime/contracts.py`
- Create: `bi_agent/runtime/recipe_registry.py`
- Create: `bi_agent/runtime/compiler.py`
- Test: `tests/phase4/test_recipe_registry_and_compiler.py`

- [ ] **Step 1: Write the failing compiler tests**

Create `tests/phase4/test_recipe_registry_and_compiler.py` with tests that assert:

```python
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.recipe_registry import load_recipe_registry


def test_registry_has_eight_recipe_entries():
    registry = load_recipe_registry()
    assert set(registry) == {
        "pattern_explanation",
        "paid_amount_change_explanation",
        "business_object_impact_review",
        "revenue_health_review",
        "segment_or_factor_attribution",
        "anomaly_or_black_swan_review",
        "custom_baseline_comparison",
        "data_quality_or_evidence_review",
    }
    assert all(registry[key].subgraph_nodes for key in registry)


def test_pattern_compile_adds_required_paths_and_records_mutations():
    registry = load_recipe_registry()
    compiled = compile_graph(
        question_family="pattern_explanation",
        target_metric="paid_amount",
        pattern_family="intra_period",
        requested_nodes=["pattern_scan"],
        registry=registry,
    )

    assert compiled.status == "accepted"
    assert {"data_quality_check", "pattern_scan", "formula_decompose", "event_evidence", "outlier_scan", "answer_verify"}.issubset(
        {node.capability for node in compiled.accepted_nodes}
    )
    assert "segment_bridge" in {node.capability for node in compiled.accepted_nodes}
    assert compiled.mutations.proposed_graph
    assert compiled.mutations.accepted_graph
    assert any(item.action == "auto_added" for item in compiled.mutations.records)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python3 -m unittest tests.phase4.test_recipe_registry_and_compiler
```

Expected: import failure for `bi_agent.runtime`.

- [ ] **Step 3: Add minimal runtime models and registry**

Implement dataclasses in `bi_agent/runtime/models.py`:

```python
@dataclass(frozen=True)
class RecipeEntry:
    recipe_id: str
    question_family: str
    subgraph_nodes: tuple[str, ...]
    default_degraded: bool = False

@dataclass(frozen=True)
class GraphNode:
    node_id: str
    capability: str
    status: str
    target_claim: str
    depends_on: tuple[str, ...] = ()

@dataclass(frozen=True)
class MutationRecord:
    action: str
    capability: str
    reason: str

@dataclass(frozen=True)
class MutationLedger:
    proposed_graph: tuple[str, ...]
    accepted_graph: tuple[str, ...]
    rejected_or_degraded: tuple[str, ...]
    records: tuple[MutationRecord, ...]

@dataclass(frozen=True)
class CompiledGraph:
    status: str
    accepted_nodes: tuple[GraphNode, ...]
    mutations: MutationLedger
```

Implement `load_recipe_registry()` with all eight recipe entries. Non-pattern entries return executable skeleton nodes and `default_degraded=True`.

- [ ] **Step 4: Add compiler**

Implement `compile_graph()` so `pattern_explanation` always keeps these evidence paths accepted or degraded with records:

```python
REQUIRED_PATTERN_PATHS = (
    "data_quality_check",
    "pattern_scan",
    "formula_decompose",
    "event_evidence",
    "segment_bridge",
    "outlier_scan",
    "answer_verify",
)
```

The compiler must record proposed graph, accepted graph, and auto-added paths. It must reject unknown capabilities and keep non-pattern recipe entries as dry-run/degraded skeletons.

- [ ] **Step 5: Run tests to verify GREEN**

Run:

```bash
python3 -m unittest tests.phase4.test_recipe_registry_and_compiler
```

Expected: `OK`.

- [ ] **Step 6: Commit**

Run:

```bash
git add requirements.txt bi_agent tests/phase4/test_recipe_registry_and_compiler.py
git commit -m "feat: add phase 4 python runtime contracts and registry"
```

Expected: runtime foundation commit.

## Task 3: ClickHouse Inspection, Binding, SQL Safety

**Files:**
- Create: `bi_agent/runtime/sql_safety.py`
- Create: `bi_agent/runtime/clickhouse_runtime.py`
- Test: `tests/phase4/test_sql_safety_and_binding.py`

- [ ] **Step 1: Write failing SQL safety tests**

Create tests that assert:

```python
from bi_agent.runtime.sql_safety import validate_select_only


def test_select_with_limit_is_allowed():
    result = validate_select_only("SELECT pay_date, sum(amount) FROM paid_success GROUP BY pay_date LIMIT 10")
    assert result.ok is True
    assert result.query_hash


def test_mutation_and_ddl_are_blocked():
    for sql in ["INSERT INTO x VALUES (1)", "DROP TABLE x", "ALTER TABLE x DELETE WHERE 1", "SELECT * FROM file('/tmp/x')"]:
        result = validate_select_only(sql)
        assert result.ok is False
        assert result.reason
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python3 -m unittest tests.phase4.test_sql_safety_and_binding
```

Expected: import failure for `bi_agent.runtime.sql_safety`.

- [ ] **Step 3: Implement SQL validator**

Implement `validate_select_only(sql: str)` using stdlib string/token checks:

- strip comments
- require the first keyword to be `SELECT` or `WITH`
- reject `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `CREATE`, `ATTACH`, `DETACH`, `SYSTEM`, `KILL`, `GRANT`, `REVOKE`
- reject ClickHouse table functions `file`, `url`, `s3`, `hdfs`, `mysql`, `postgresql`, `jdbc`, `odbc`
- require `LIMIT` for inspection/sample queries unless the caller marks the query as aggregate
- return SHA-256 hash, never secrets

- [ ] **Step 4: Add ClickHouse runtime shell**

Implement `ClickHouseRuntime.from_env()` reading:

```text
WAJE_CLICKHOUSE_HOST
WAJE_CLICKHOUSE_PORT
WAJE_CLICKHOUSE_USER
WAJE_CLICKHOUSE_PASSWORD
WAJE_CLICKHOUSE_DATABASE
WAJE_CLICKHOUSE_SECURE
```

Implement:

- `configured()`
- `show_tables()`
- `describe_table(table_name)`
- `sample_rows(table_name, limit=5)`
- `aggregate(sql, query_id)`

Every method must pass through `validate_select_only` or the explicit `SHOW/DESCRIBE` allowlist. Missing env returns a runtime binding failure object; it must not guess schema.

- [ ] **Step 5: Run tests to verify GREEN**

Run:

```bash
python3 -m unittest tests.phase4.test_sql_safety_and_binding
```

Expected: `OK`.

- [ ] **Step 6: Commit**

Run:

```bash
git add bi_agent/runtime/sql_safety.py bi_agent/runtime/clickhouse_runtime.py tests/phase4/test_sql_safety_and_binding.py
git commit -m "feat: add clickhouse inspection and sql safety"
```

Expected: ClickHouse validator commit.

## Task 4: Pattern Capabilities And Evidence Payloads

**Files:**
- Create: `bi_agent/capabilities/data_quality_check.py`
- Create: `bi_agent/capabilities/pattern_scan.py`
- Create: `bi_agent/capabilities/formula_decompose.py`
- Create: `bi_agent/capabilities/event_evidence.py`
- Create: `bi_agent/capabilities/segment_bridge.py`
- Create: `bi_agent/capabilities/joint_attribution.py`
- Create: `bi_agent/capabilities/outlier_scan.py`
- Test: `tests/phase4/test_pattern_scan.py`

- [ ] **Step 1: Write failing pattern tests**

Create a fixture with monthly phase aggregates and assert:

```python
from bi_agent.capabilities.pattern_scan import scan_pattern


def test_month_start_pattern_requires_direction_and_uplift():
    rows = [
        {"month": "2024-01", "phase": "start", "amount": 110, "days": 10},
        {"month": "2024-01", "phase": "mid", "amount": 90, "days": 10},
        {"month": "2024-01", "phase": "end", "amount": 95, "days": 11},
    ] * 24

    result = scan_pattern(rows, pattern_family="intra_period", target_phase="start", materiality_floor=0.03)

    assert result.established is True
    assert result.direction_ratio >= 0.70
    assert result.median_uplift >= 0.03
    assert result.evidence_type == "statistical_association"
    assert result.strength in {"medium", "high"}


def test_weak_direction_degrades_pattern_claim():
    rows = [
        {"month": "2024-01", "phase": "start", "amount": 100, "days": 10},
        {"month": "2024-01", "phase": "mid", "amount": 120, "days": 10},
        {"month": "2024-01", "phase": "end", "amount": 95, "days": 11},
    ] * 24

    result = scan_pattern(rows, pattern_family="intra_period", target_phase="start", materiality_floor=0.03)

    assert result.established is False
    assert result.wording_limit in {"tendency", "insufficient"}
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python3 -m unittest tests.phase4.test_pattern_scan
```

Expected: import failure for `bi_agent.capabilities.pattern_scan`.

- [ ] **Step 3: Implement generalized pattern scanner**

Implement `scan_pattern()` with pure Python aggregates:

- intra-period: compare target phase with sibling phases by month
- weekly: compare selected weekday group with week average
- event-relative: compare before/during/after windows
- rolling: compare rolling windows against baseline band
- lag/recovery: compare post-event lag buckets
- custom-baseline: compare target rows to named baseline rows

For month-start regression:

- complete comparable months threshold: `>= 24`
- direction ratio established: `>= 0.70`
- tendency: `0.60 <= ratio < 0.70`
- uplift: target phase versus the higher sibling phase
- exceptions: every failed direction, incomplete, or outlier-dominated period

- [ ] **Step 4: Add degraded capability skeletons**

Implement formula, event, segment, joint, outlier, and data-quality functions returning evidence envelopes with:

- `evidence_ref`
- `capability`
- `evidence_type`
- `strength`
- `wording_limit`
- `typed_payload`
- `limitations`
- `result_refs`

`formula_decompose` attempts all current-data-covered formula paths and returns degraded gaps when components are unavailable. `segment_bridge` runs before `joint_attribution`; joint attribution only runs when residual or fit requires escalation.

- [ ] **Step 5: Run tests to verify GREEN**

Run:

```bash
python3 -m unittest tests.phase4.test_pattern_scan
```

Expected: `OK`.

- [ ] **Step 6: Commit**

Run:

```bash
git add bi_agent/capabilities tests/phase4/test_pattern_scan.py
git commit -m "feat: add pattern capabilities and evidence payloads"
```

Expected: capability commit.

## Task 5: LangGraph Workflow, Artifacts, Answer Package, Visibility

**Files:**
- Create: `bi_agent/runtime/langgraph_workflow.py`
- Create: `bi_agent/runtime/artifacts.py`
- Create: `bi_agent/runtime/answer_package.py`
- Create: `bi_agent/runtime/wording.py`
- Modify: `.gitignore`
- Test: `tests/phase4/test_workflow_artifacts_answer.py`

- [ ] **Step 1: Write failing workflow and artifact tests**

Create tests that assert:

```python
from bi_agent.runtime.artifacts import filter_artifact_for_role
from bi_agent.runtime.langgraph_workflow import run_pattern_workflow


def test_langgraph_failure_does_not_publish_business_conclusion():
    result = run_pattern_workflow({"force_langgraph_failure": True})
    assert result.status == "failed"
    assert result.answer_package is None
    assert result.failure_reason


def test_role_visibility_hides_admin_sql_from_business_reader():
    artifact = {
        "sections": [
            {"section_id": "summary", "visibility": "business_summary", "payload": {"text": "draft"}},
            {"section_id": "sql", "visibility": "admin_audit", "payload": {"sql": "SELECT 1"}},
        ]
    }
    filtered = filter_artifact_for_role(artifact, "business_reader")
    assert [section["section_id"] for section in filtered["sections"]] == ["summary"]
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python3 -m unittest tests.phase4.test_workflow_artifacts_answer
```

Expected: import failure for `bi_agent.runtime.langgraph_workflow`.

- [ ] **Step 3: Implement LangGraph workflow**

Build a `StateGraph` with these nodes:

1. `intent_binding`
2. `compile_graph`
3. `inspect_schema`
4. `validate_runtime_binding`
5. `execute_capabilities`
6. `synthesize_draft_answer`
7. `answer_verify`
8. `persist_artifact`

Rules:

- every node writes a checkpoint event
- technical failures can retry once
- business, evidence, permission, contract, and SQL failures do not retry
- LangGraph execution failure returns failed run state and no local business-conclusion fallback
- accepted graph, proposed graph, rejected/degraded mutations, and validator results enter the artifact

- [ ] **Step 4: Implement artifacts and visibility**

Persist JSON under `artifacts/phase-4/<run_id>/answer_package.json`.

Visibility:

- `business_reader`: `business_summary`, `aggregate_evidence`
- `analyst`: `business_summary`, `aggregate_evidence`, `diagnostic_detail`
- `data_owner_admin`: all sections, including `admin_audit`

Admin audit may include SQL text, SQL hash, validator results, and artifact audit. Ordinary sections include SQL hash only.

- [ ] **Step 5: Implement answer verifier and wording warnings**

Verifier checks:

- evidence refs exist
- numbers in draft claims match evidence payload values
- scope/time/window match evidence
- causal wording is absent unless `causal_evidence`
- coverage and missing-contract limitations are visible
- wording violations are warnings in admin audit and do not block Phase 4 draft eval

- [ ] **Step 6: Run tests to verify GREEN**

Run:

```bash
python3 -m unittest tests.phase4.test_workflow_artifacts_answer
```

Expected: `OK`.

- [ ] **Step 7: Commit**

Run:

```bash
git add .gitignore bi_agent/runtime/langgraph_workflow.py bi_agent/runtime/artifacts.py bi_agent/runtime/answer_package.py bi_agent/runtime/wording.py tests/phase4/test_workflow_artifacts_answer.py
git commit -m "feat: wire langgraph workflow and answer artifacts"
```

Expected: workflow and artifact commit.

## Task 6: Phase 4 Eval Harness And CLI

**Files:**
- Create: `evals/phase4/pattern_cases.yaml`
- Create: `tools/phase4/run_phase4_pattern_slice.py`
- Create: `tools/phase4/validate_phase4.py`
- Test: `tests/phase4/test_phase4_eval_harness.py`

- [ ] **Step 1: Write failing eval harness test**

Create tests that assert:

```python
from tools.phase4.validate_phase4 import run_fixture_eval


def test_phase4_fixture_eval_requires_month_start_and_two_siblings():
    result = run_fixture_eval()
    assert result.engineering_fixture_passed is True
    assert result.month_start_case.status == "passed"
    assert result.sibling_summary.passed_count >= 2
    assert all(case.reason for case in result.sibling_summary.degraded_or_blocked)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python3 -m unittest tests.phase4.test_phase4_eval_harness
```

Expected: import failure for `tools.phase4`.

- [ ] **Step 3: Add eval cases**

Create `evals/phase4/pattern_cases.yaml` with five cases:

- month-start regression: 2024-01 to 2026-05, 1-10 / 11-20 / 21-end
- weekly sibling
- event-relative sibling
- rolling sibling
- custom-baseline sibling

Each case must declare:

- `case_id`
- `pattern_family`
- `window_definition`
- `required_capabilities`
- `minimum_real_data_status`
- `expected_degraded_or_blocked_reasons`
- `fixture_rows`

- [ ] **Step 4: Add CLI**

`tools/phase4/run_phase4_pattern_slice.py` accepts:

```bash
python3 tools/phase4/run_phase4_pattern_slice.py --case month_start --mode real
python3 tools/phase4/run_phase4_pattern_slice.py --case month_start --mode fixture
```

Rules:

- real mode uses ClickHouse env vars
- missing ClickHouse config fails runtime binding and writes no business conclusion
- fixture mode writes artifact marked `non_real_data: true`
- SQL secrets are never printed

- [ ] **Step 5: Add validator**

`tools/phase4/validate_phase4.py` runs:

```bash
python3 -m unittest discover -s tests/phase4
ruby tools/evals/validate-phase-3.rb
git diff --check
```

It then runs fixture eval. If real ClickHouse env vars are present, it runs real month-start eval. If absent, it reports `external_dependency_blocked` with owner `data_engineering_owner` and repair path `provide read-only ClickHouse env vars and accepted physical binding`.

- [ ] **Step 6: Run tests to verify GREEN**

Run:

```bash
python3 -m unittest tests.phase4.test_phase4_eval_harness
```

Expected: `OK`.

- [ ] **Step 7: Commit**

Run:

```bash
git add evals/phase4 tools/phase4 tests/phase4/test_phase4_eval_harness.py
git commit -m "test: add phase 4 eval harness"
```

Expected: eval harness commit.

## Task 7: Closeout Validation And Status Doc

**Files:**
- Create: `docs/phase-4-closeout-status.md`

- [ ] **Step 1: Run Phase 4 validation**

Run:

```bash
python3 tools/phase4/validate_phase4.py
```

Expected:

- Python unit tests pass
- Phase 3 Ruby validators still pass, or any local external dependency block is recorded
- fixture eval passes
- real month-start eval passes when ClickHouse env and physical binding exist
- real eval is marked blocked when env or binding is missing

- [ ] **Step 2: Run existing frontend build check**

Run:

```bash
npm run build
```

Expected: Next build passes, or unrelated pre-existing build failure is recorded with exact error.

- [ ] **Step 3: Write closeout doc**

Create `docs/phase-4-closeout-status.md` with:

- completed engineering items
- validation commands and outputs
- artifact path examples
- real-data blockers with owner, current block, and repair path
- explicit statement that month-start is a regression case for generalized `pattern_explanation`
- explicit statement that LangGraph failure produces no local business-conclusion fallback

- [ ] **Step 4: Commit**

Run:

```bash
git add docs/phase-4-closeout-status.md
git commit -m "docs: close out phase 4 vertical slice"
```

Expected: closeout commit.

## Final Verification

Run:

```bash
python3 tools/phase4/validate_phase4.py
npm run build
git status --short
```

Expected:

- validation passes or lists only external dependency blockers with owner and repair path
- frontend build passes
- working tree is clean

## Self-Review

- Spec coverage: the plan covers LangGraph workflow, recipe registry, eight subgraph skeletons, ClickHouse inspection/binding, SQL safety, generalized pattern runtime, evidence refs, artifacts, role visibility, wording policy, verifier, evals, and closeout.
- Placeholders: no step depends on an unnamed future task.
- Type consistency: `CompiledGraph`, `GraphNode`, evidence envelopes, artifact sections, and eval result objects are named once and reused consistently.
