# Phase 4 First Pattern Vertical Slice Closeout

Date: 2026-07-06

## Status

Phase 4 has a runnable draft vertical slice for generalized `pattern_explanation`.
The month-start paid amount question is covered as a regression case for the
general pattern runtime, alongside weekly, event-relative, rolling, and
custom-baseline sibling cases.

The runtime uses the Python BI Agent Core with a real LangGraph workflow and an
OpenAI-compatible LLM adapter. Local `.env` is configured for DeepSeek
`deepseek-v4-flash`; secrets stay out of git and normal artifacts. If LangGraph
execution fails, the run returns failure and does not publish a local business
conclusion.

The main agent workflow reference is versioned in
`docs/phase-4-agent-workflow-reference.md`. The main workflow is the fixed
runtime lifecycle. The accepted graph is the per-question business analysis
graph compiled from LLM proposals, contracts, policy, permissions, budget, and
evidence requirements.

## Completed Engineering Items

- Installed and pinned Python runtime dependencies in `requirements.txt`,
  including `langgraph`, `clickhouse-connect`, and `PyYAML`.
- Added Python runtime contracts, recipe registry, graph compiler skeleton, and
  eight recipe entries with executable or degraded subgraph paths.
- Added ClickHouse read-only runtime binding, env-based config, inspection
  helpers, and SQL safety validation for SELECT-only aggregate/sample queries.
- Added generalized pattern scanning for intra-period, weekly, event-relative,
  rolling, lag/recovery, and custom-baseline families.
- Wired required evidence paths: data quality, pattern scan, formula
  decomposition, event evidence, segment bridge, outlier scan, joint
  attribution placeholder, and answer verification.
- Added LangGraph workflow checkpoints, retry classification, graph mutation
  ledgers, answer package draft generation, local artifact persistence, and
  role-based artifact filtering.
- Replaced the earlier deterministic workflow skeleton with LLM-backed nodes for
  business intent, boundary decision, clarification, route design, route repair,
  data coverage interpretation, next action selection, promotion direction,
  evidence interpretation, answer synthesis, semantic audit, answer repair, and
  blocked/degraded explanations.
- Added LLM call audit records with task, provider, model, prompt version,
  response id, input/output hashes, usage, and structured output in the admin
  audit section.
- Added local evidence policy gates so LLM route decisions cannot downgrade a
  supported pattern answer only because mechanism, event, outlier, or
  attribution evidence is missing.
- Restricted ordinary artifact views to SQL hash only; SQL text, validator
  results, graph details, checkpoints, verifier output, and artifact audit stay
  in the `data_owner_admin` view.
- Added wording warnings into admin audit without blocking Phase 4 draft eval.
- Added Phase 4 eval harness and CLI covering fixture mode and real ClickHouse
  mode.
- Added a 2026H1 real-data pattern suite using the accepted clean ClickHouse
  table.

## Validation

`python3 tools/phase4/validate_phase4.py`

- Phase 4 Python tests: passed, 56 tests.
- Phase 3 Ruby validators: passed.
- Contract validation: passed, 21 YAML files parsed.
- Launch eval validation: passed, 8 expectation packages.
- `git diff --check`: passed.
- Fixture eval: passed all 5 cases.
- 2026H1 real-data eval: passed expected statuses for 5 cases:
  - `weekly_thu_fri_vs_mon_sun`: passed.
  - `custom_q2_vs_q1`: passed.
  - `rolling_28_day_growth`: passed.
  - `month_boundary_vs_mid`: degraded as expected.
  - `strict_month_start_2026h1`: degraded as expected.
- Real month-start eval: local ClickHouse env and physical SQL binding are
  configured. The run reaches ClickHouse and writes a real artifact, then blocks
  because the accepted clean table covers 2026-01 through 2026-06 while the
  regression asks for 2024-01 through 2026-05.

The validation run also verifies that evaluated Answer Packages include the
required LLM audit path. The answer verifier remains local and checks evidence
refs, numeric claims, scope, time window, and visible limitations.

`npm run build`

- Next.js production build passed.
- Routes built: `/`, `/_not-found`, `/api/langgraph`, `/icon.svg`.

## Artifact Examples

Generated local artifacts are ignored by git under `artifacts/phase-4/`.

- `artifacts/phase-4/phase4-fixture-month_start/answer_package.json`
- `artifacts/phase-4/phase4-fixture-weekly_payday_like/answer_package.json`
- `artifacts/phase-4/phase4-fixture-event_relative_campaign/answer_package.json`
- `artifacts/phase-4/phase4-fixture-rolling_recovery/answer_package.json`
- `artifacts/phase-4/phase4-fixture-custom_baseline_release/answer_package.json`
- `artifacts/phase-4/phase4-real-weekly_thu_fri_vs_mon_sun/answer_package.json`
- `artifacts/phase-4/phase4-real-custom_q2_vs_q1/answer_package.json`
- `artifacts/phase-4/phase4-real-rolling_28_day_growth/answer_package.json`
- `artifacts/phase-4/phase4-real-month_boundary_vs_mid/answer_package.json`
- `artifacts/phase-4/phase4-real-strict_month_start_2026h1/answer_package.json`

Fixture artifacts are marked with `non_real_data: true`. Real-data artifacts are
marked with `non_real_data: false`; degraded real guardrail cases do not publish
business conclusions.

## Real Data Blockers

Owner: `data_engineering_owner`

Current local setup:

- local read-only ClickHouse user: configured in `.env`
- accepted Phase 4 SQL binding: configured in `.env` as
  `WAJE_PHASE4_PATTERN_SQL`
- clean table: `waje_bi.paid_order_success_clean`
- actual covered data window: 2026-01-01 through 2026-06-30

Current block: the month-start regression expects the 2024-01 through 2026-05
window. The available accepted clean table has only 5 comparable months inside
that requested window, so the real run returns `external_dependency_blocked`
with owner `data_engineering_owner` and does not publish a business conclusion.

Repair path: load or bind enough accepted successful-payment history for
2024-01 through 2026-05, then rerun:

```bash
python3 tools/phase4/run_phase4_pattern_slice.py --case month_start --mode real
python3 tools/phase4/validate_phase4.py
```

## Closeout Notes

- Month-start remains a generalized `pattern_explanation` regression case.
- Fixture eval is allowed to pass for engineering coverage, but it is explicitly
  marked non-real.
- Real query failure returns failure or external dependency block and does not
  publish a business conclusion.
- Ordinary business output exposes SQL hash only.
