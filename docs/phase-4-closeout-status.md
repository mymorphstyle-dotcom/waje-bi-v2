# Phase 4 First Pattern Vertical Slice Closeout

Date: 2026-07-06

## Status

Phase 4 has a runnable draft vertical slice for generalized `pattern_explanation`.
The month-start paid amount question is covered as a regression case for the
general pattern runtime, alongside weekly, event-relative, rolling, and
custom-baseline sibling cases.

The runtime uses the Python BI Agent Core with a real LangGraph workflow. If
LangGraph execution fails, the run returns failure and does not publish a local
business conclusion.

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
- Restricted ordinary artifact views to SQL hash only; SQL text, validator
  results, graph details, checkpoints, verifier output, and artifact audit stay
  in the `data_owner_admin` view.
- Added wording warnings into admin audit without blocking Phase 4 draft eval.
- Added Phase 4 eval harness and CLI covering fixture mode and real ClickHouse
  mode.

## Validation

`python3 tools/phase4/validate_phase4.py`

- Phase 4 Python tests: passed, 43 tests.
- Phase 3 Ruby validators: passed.
- Contract validation: passed, 21 YAML files parsed.
- Launch eval validation: passed, 8 expectation packages.
- `git diff --check`: passed.
- Fixture eval: passed all 5 cases.
- Real month-start eval: blocked by missing local ClickHouse env vars, recorded
  as `external_dependency_blocked` with owner `data_engineering_owner`.

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

Fixture artifacts are marked with `non_real_data: true` and
`business_conclusion_published: false` in eval output.

## Real Data Blockers

Owner: `data_engineering_owner`

Current block: local environment does not provide the required read-only
ClickHouse variables:

- `WAJE_CLICKHOUSE_HOST`
- `WAJE_CLICKHOUSE_PORT`
- `WAJE_CLICKHOUSE_USER`
- `WAJE_CLICKHOUSE_PASSWORD`
- `WAJE_CLICKHOUSE_DATABASE`
- `WAJE_CLICKHOUSE_SECURE`

Real mode also requires an accepted physical aggregate SQL binding in
`WAJE_PHASE4_PATTERN_SQL`. The SQL must pass the SELECT-only aggregate
validator. When provided, the executed SQL hash is persisted in the admin audit.

Repair path: provide read-only ClickHouse env vars and an accepted physical
binding for the cleaned successful payment fact table, then rerun:

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
