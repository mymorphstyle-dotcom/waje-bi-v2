# General Agent Runtime P3 target-environment delivery ledger

This ledger is the delivery authority for applying the accepted P3 contract to
the environment configured by `/Users/luka/work/waje-bi-v2/.env`. A row moves to
`complete` only after the named mutation or observation has durable evidence and
its verification command passes. Secrets, raw Provider payloads, customer rows,
and database credentials are excluded from this document.

| ID | Status | Delivery item | Completion evidence |
| --- | --- | --- | --- |
| ENV-01 | complete | Target identity and supported source migration | Read-only preflight found exactly one accepted `single-authority-workflow.v9` migration, 98 runtime tables, and no process-level `OPENAI_API_KEY`. `127.0.0.1:15432` resolves to the PostgreSQL cluster in `waje-bi-postgres`; the configured database is `waje_bi_runtime`. |
| ENV-02 | complete | Immutable snapshot-consistent pre-upgrade backup | Created `artifacts/runtime-backups/waje-runtime-v9-before-v12-20260722T115505+0800.zip` from one exported PostgreSQL snapshot. The mode is `0600`, size is 148453109 bytes, SHA-256 is `19d12b4657f31fec1a5ddc95f80588abf66da530fc5a6722445adaceeb9f8d42`, and the verified archive contains 98 tables / 115339 rows plus the v9 migration identity. `unzip -t` and independent digest verification passed. |
| ENV-03 | complete | Transactional v9→v12 in-place upgrade | The configured `waje_bi_runtime` database upgraded from the accepted v9 identity to `single-authority-workflow.v12` / digest `eb21d255d9bec86b8a98ab5c2693b237b473357c37aa355cc1a605474411bfa3`. The cutover compared every existing business-table row count before commit; all additive tables remained empty. |
| ENV-04 | complete | Post-upgrade read-only database gate | The target database passed the `REPEATABLE READ READ ONLY` v12 gate. It exposes all required tables and append-only triggers, the current `tool_selection` item contract, 34773 audit records, and 14 retained historical audit/thread orphans under the enforced `NOT VALID` foreign-key contract. Deployment, backup, and cutover connections now bind explicit `waje.actor_id=system`. |
| ENV-05 | complete | Prebuilt Python and continuous recovery worker | `.env` points to the executable project `.venv/bin/python`; `sys.prefix`, psycopg 3.3.4, and executable checks passed. A bounded worker cycle and multiple continuous cycles completed against v12 with no recoverable work, then continuous mode handled SIGINT and exited 0. The 30-day trace-prune command also completed and deleted zero records. |
| ENV-06 | complete | Production identity and readiness secrets | Generated independent 64-byte local HMAC/readiness secrets in the mode-0600 ignored `.env`. A production server returned public liveness 200, denied readiness without the token as 404, and returned readiness 200 with PostgreSQL/config checks. A correctly signed new actor read an empty thread projection; a query-path mutation using the original signature failed 401 with the customer-safe `sign_in_required` code. |
| ENV-07 | complete | Runtime capacity and SSE configuration | `.env` explicitly sets the accepted process limits 16/2, SSE limits 128/4, SSE TTL/poll 300000/2000 ms, worker poll 2 s, and dispatch lease 30000 ms. Configuration validators and TypeScript checks passed. Production HTTP also returned nonce CSP with all 24 scripts bound to the response nonce, HSTS, and frame denial. |
| ENV-08 | complete | Mainland-only model egress and WAJE-only trace | The no-`OPENAI_API_KEY` full gate observed five Provider requests. Their only origin was `https://api.deepseek.com` and their only path was `/chat/completions`; all 13 declared capability checks passed. Nine SDK traces / 62 records reached the single `WajeTraceProcessor`, and `openaiExporterUsed` was false. The application exact-origin guard and report scan reject `api.openai.com`; host-wide firewall policy remains deployment-infrastructure scope. |
| ENV-09 | complete | Complete deployment report and regression closure | Saved the passing `general-agent-deployment.v1` report at `artifacts/deployment-reports/general-agent-deployment-v12-20260722.json`. Phase 7/8 returned 1541 passed / 40 skipped; Playwright returned 13 passed; TypeScript, Next.js production build, Python compileall, release-manifest v31 validation, current active-ref validation, and `git diff --check` passed. npm audit and pip-audit each reported zero known vulnerabilities. |

## Mutation boundary

- The source database is modified only after ENV-02 is complete.
- The upgrade runs in one database transaction and must preserve every existing
  business-table row count.
- A failed upgrade rolls back and leaves the v9 migration identity active.
- Case B is outside this delivery and will not be created or rerun.
