# Local ClickHouse storage and write-pressure audit — 2026-07-26

## Outcome

The no-downtime configuration stage and the approved cutover are complete.
`waje-bi-clickhouse` is healthy on external named volume
`waje-bi-clickhouse-data-v3`, with restart policy `unless-stopped`, a 12-CPU
cap, and an 18 GiB memory cap. The original bind directory and both
failed-attempt volumes remain intact; no data or volume was deleted.

The complete machine-readable pre-migration evidence is saved at:

`/Users/luka/work/waje-bi-v2/output/clickhouse-local-optimization/pre-migration-20260726T144100+0800`

Important files inside that directory:

- `baseline.json`
- `databases.json`
- `business-tables.tsv`
- `system-logs.tsv`
- `system-log-definitions.json`
- `docker-mount.json`
- `docker-inspect.redacted.json`
- `clickhouse-config/`
- `compose-rendered.yaml`
- `commands.md`
- `system-log-growth-104s.json`
- `representative-query-baseline/representative-query.json`
- `clickhouse-24.8-isolated-config-probe.txt`
- `copy-helper-tmpfs-probe.txt`
- `pytest-clickhouse-related.txt`
- `pytest-clickhouse-ops.txt`
- `typescript-noemit.txt`
- `next-build.txt`

## Pre-migration state

- Repository: `/Users/luka/work/waje-bi-v2`
- Branch / audited HEAD: `codex/agents-sdk-p0` /
  `d2205a49b5cc19ca53dc970d997c7834e457edf9`
- Existing unrelated user changes: preserved
- ClickHouse: `24.8.14.39`, healthy and queryable
- Container creation mode: standalone `docker run`; no WAJE Compose labels
- Data mount: bind
  `/Users/luka/work/waje-bi-v2/data/clickhouse` →
  `/var/lib/clickhouse`
- Data size: about 16 GiB, about 36,000 host files
- WAJE named ClickHouse volumes before change: none
- Container limits / healthcheck: none / none
- Observed idle memory / PID count: about 2.22 GiB / 960
- Docker Desktop allocation: 18 CPUs / 20 GiB
- Host swap: about 19 GiB used
- `fseventsd`: abnormal CPU and about 4.7 GiB RSS during the audit sample

The only other WAJE database container is `waje-bi-postgres`; it uses the
existing `data/postgres` bind. It is outside this storage cutover.

## Database and business-table baseline

Databases:

- `INFORMATION_SCHEMA`
- `default`
- `information_schema`
- `system`
- `waje_bi`

There are 28 business tables: 25 MergeTree and 3 TinyLog. Exact rows, active
parts, and bytes on disk for every table are in `business-tables.tsv`. The
captured totals are 309,376,869 rows and 15,087,367,103 bytes.

Largest tables:

| Database / table | Rows | Active parts | Bytes on disk |
|---|---:|---:|---:|
| `waje_bi.paid_order_detail_raw_20240101_20260704` | 75,984,922 | 1 | 4,413,583,094 |
| `waje_bi.paid_order_success_clean_20240101_20260704` | 41,234,677 | 2 | 2,571,004,429 |
| `waje_bi.paid_order_success_clean_20240101_20260704_v2` | 41,234,677 | 7 | 2,465,795,042 |
| `waje_bi.paid_order_detail_raw` | 38,941,583 | 2 | 2,335,964,324 |
| `waje_bi.paid_order_success_clean` | 23,858,847 | 1 | 1,497,977,084 |
| `waje_bi.paid_order_success_latest_key_20240101_20260704` | 41,234,677 | 4 | 716,564,985 |
| `waje_bi.paid_order_success_latest_key_20240101_20260704_v2` | 41,234,677 | 4 | 716,562,916 |

At capture time `system.merges` was empty. Two historic mutations were present,
both complete with zero parts remaining.

## System-log baseline

The stock image config has no active TTL on these existing tables:

| System table | Rows | Bytes on disk |
|---|---:|---:|
| `asynchronous_metric_log` | 1,635,227,262 | 261,870,737 |
| `text_log` | 15,988,323 | 595,855,975 |
| `trace_log` | 17,316,157 | 331,786,578 |
| `metric_log` | 1,801,304 | 235,042,076 |
| `processors_profile_log` | 1,725,579 | 48,679,130 |
| `query_log` | 31,689 | 7,856,593 |
| `part_log` | 1,994 | 140,810 |
| `error_log` | 108 | 1,651 |

Across the eight tables, 1,672,092,416 rows occupy 1,481,233,550 bytes.

During one 104-second window with the eval runner stopped:

- `asynchronous_metric_log`: +89,892 rows;
- `text_log`: +916 rows;
- `trace_log`: +235 rows;
- `processors_profile_log`: +95 rows;
- `metric_log`: +99 rows;
- `query_log`: +4 rows from audit queries.

The stock profile records both query CPU and real-time samples every second,
memory stack traces at each 4 MiB step, and processor profiles. The server
logger and `text_log` are set to `trace`. These settings explain the continuing
idle writes.

## Query and resource evidence

The seven-day sample has 8,860 completed selects:

| Metric | P50 | P95 | P99 | Max |
|---|---:|---:|---:|---:|
| Memory | 27.46 MiB | 2.25 GiB | 2.54 GiB | 12.49 GiB |
| Duration | 270 ms | 7,218 ms | 14,065 ms | 534,850 ms |
| Read rows | — | 82,469,354 | — | 536,050,801 |

The fixed pre-migration benchmark read 41,234,677 rows / 1,030,866,925 bytes,
completed in 1,873 ms (2.028 seconds wall), used 508,369,849 bytes peak memory,
wrote zero business rows/bytes, and recorded 12,288 OS write bytes for logging.

This evidence supports a 16 GiB server ceiling inside an 18 GiB container. A
lower 8–12 GiB limit would put observed queries at risk. The concurrency and
thread-pool defaults are excessive for this local workload: 1,000 concurrent
queries, 10,000 global threads with 1,000 retained, and an effective 512-thread
background scheduler.

## Insert and rebuild audit

All ClickHouse write entry points are under `tools/data`:

- the raw payment ZIP loader streams each source CSV as one
  `CSVWithNames` insert;
- payment-order/bet ingestion uses 5,000-row batches;
- gameplay ingestion uses 10,000-row batches;
- market dashboard ingestion submits one JSONEachRow block per snapshot;
- final-outcome and derived payment tables use set-based `INSERT SELECT`;
- TinyLog metadata inserts contain only the bounded metadata record set.

No application runtime writes analytical events row by row. No `OPTIMIZE FINAL`
appeared in the 30-day query log.

One reusable failure family was found in
`clean_payment_details_clickhouse.py`: `--skip-load` still unconditionally
TRUNCATED and rebuilt five derived tables. Query history contains 22 truncations
of the clean table and 17 raw-table drops. The repaired command contract makes
reuse read-only by default; `--rebuild-derived` is required to authorize the
TRUNCATE/rebuild path. A fresh source load still performs the required
set-based rebuild.

## Failure families and general fixes

| Failure family | Root cause / impact | General fix | Regression evidence |
|---|---|---|---|
| Host path amplification | MergeTree temporary parts, renames, and deletes occur under a watched macOS path | Named-volume storage contract; preserve old bind as rollback | Compose render plus mount-aware migration/verify tool |
| Observability over-collection | Stock trace, metric, async metric, processor and trace-level text logging | Bounded development log policy; retain SQL/error/part evidence | Real 24.8 tmpfs startup and `SYSTEM FLUSH LOGS` probe |
| Resource fan-out | Server defaults assume a much larger/concurrent environment | Evidence-sized CPU, memory, global/background pool and MergeTree thresholds | 24.8 effective-setting query and startup sanity check |
| Rebuild authority conflation | `--skip-load` reused raw data while silently authorizing destructive derived rebuild | Explicit `--rebuild-derived` state | Focused Python tests |
| Missing cutover contract | Standalone container had no repeatable health, migration, verification, or rollback path | Pinned Compose plus gated, idempotent operations tool | dry-run against current bind; mutation flags tested |
| Host daemon after-effect | Existing `fseventsd` pressure can survive repository changes | Separate host recovery; use path events and write deltas for acceptance | Deferred to post-cutover host observation |

## Implemented files

- `compose.clickhouse.yaml`
- `ops/clickhouse/config.d/90-waje-development.xml`
- `ops/clickhouse/users.d/90-waje-development.xml`
- `tools/clickhouse/local_storage.py`
- `tools/data/clean_payment_details_clickhouse.py`
- `tests/tools/test_clickhouse_local_storage.py`
- `tests/phase4/test_paid_success_first_payment_repair.py`
- `.vscode/settings.json`
- `.gitignore`
- `docs/runbooks/local-clickhouse-storage.md`

The isolated ClickHouse 24.8 probe started successfully with the new settings.
After `SYSTEM FLUSH LOGS`, only `query_log`, `part_log`, `text_log`, and
`error_log` existed; their TTLs were 14, 14, 7, and 30 days. The four disabled
high-frequency tables were absent.

Validation completed before cutover:

- `docker compose -f compose.clickhouse.yaml config`: passed;
- disposable ClickHouse 24.8 startup/effective-setting/log-DDL probe: passed;
- disposable UID/GID/mode/link/file-content copy-manifest probe: passed;
- 156 ClickHouse data-contract/ingestion tests: passed in 234.23 seconds;
- 9 local storage operations tests: passed;
- `tsc --noEmit`: passed;
- `next build`: passed with Next.js 16.2.11;
- Python compile, JSON parse, `git diff --check`, and migration dry-run: passed.

The first cutover attempt on 2026-07-26 failed closed before container
replacement because the initial manifest included filesystem-specific directory
inode sizes. The tool automatically restarted the original bind-backed
container. `waje-bi-clickhouse-data` is retained unchanged as that attempt's
snapshot.

The second attempt copied into `waje-bi-clickhouse-data-v2` and again failed
closed before replacement. A field-level read-only diff found zero type
differences across common paths, 27,764 UID differences, 27,764 GID
differences, and 47 mode differences. The mode differences were ClickHouse
database symlinks. A direct probe also showed a single macOS bind file as
`0:0` to a root process and `101:101` to the ClickHouse process, while the host
owner is `501:20`. Docker Desktop's bind ownership projection and symlink mode
therefore cannot serve as portable per-path equality fields.

The v3 copy contract keeps exact path/type, non-link mode, file mtime,
symbolic-link-target, and per-file-content SHA-256 validation. It normalizes the
Linux target to the image runtime user and requires zero owner mismatches,
unreadable files, or unsearchable directories before cutover. ClickHouse then
remains the semantic authority for database/table set, row counts, parts,
queries, and persisted writes. Both failed volumes are preserved; the active
cutover target is `waje-bi-clickhouse-data-v3`.

## Completed v3 acceptance

The v3 cutover completed on 2026-07-26. The copy manifest passed all portable
strong checks:

- path/type SHA-256:
  `68e45f5b4095426fe02611d1f601c603a214e30b526d04a870d5de5f4ae162d4`;
- non-link mode SHA-256:
  `271a35994cc5d8646fb76d16e2a4187541c14f43ea80b0fabaf52e74a22d7f3e`;
- file-mtime SHA-256:
  `1bde6376b9927f5f1284acf24ee5bea1974dad94e340f04592d98f6c8383b594`;
- file-content SHA-256:
  `c3065b31dc61130892680a97f383c7c967dbe065e989ff314b6499aafd16fd0a`;
- symbolic-link-target SHA-256:
  `e6af90556d3d9163ede1c989a75633ab05d1d838fec2f91c366c3f78d1998ba2`;
- target runtime owner `101:101`, with zero owner mismatches, unreadable
  files, or unsearchable directories.

Docker inspect confirms `/var/lib/clickhouse` has `Type=volume` and
`Name=waje-bi-clickhouse-data-v3`. Compose config, healthcheck, and restart
policy passed. All five databases and 28 business tables are queryable.
Migration before/after totals are identical:

| Measure | Before | After |
|---|---:|---:|
| Business rows | 309,376,869 | 309,376,869 |
| Active parts | 50 | 50 |
| Bytes on disk | 15,087,367,103 | 15,087,367,103 |

Every table has the same row count. Every table also retained the same active
parts and bytes at the cutover comparison point. The test-owned write probe
persisted through a container restart, was read successfully, and was dropped;
no probe table remains.

In the same 104-second observation length, the disabled high-frequency logs
changed as follows:

| Log | Before rows | After rows |
|---|---:|---:|
| `asynchronous_metric_log` | +89,892 | 0 |
| `metric_log` | +99 | 0 |
| `processors_profile_log` | +95 | 0 |
| `trace_log` | +235 | 0 |

The retained post-cutover logs recorded `query_log` +54, `text_log` +8, and
`error_log` +1. The text rows are restart-time platform warnings. The error
aggregate was generated before the measurement window and flushed inside it;
it includes the rejected audit query that referenced a non-existent
`system.tables.rows` field. The corrected inventory uses exact per-table
`count()` values.

The fixed 41,234,677-row representative query returned the identical result
and read the identical 1,030,866,925 bytes:

| Measure | Before | After |
|---|---:|---:|
| Query duration | 1,873 ms | 1,189 ms |
| Wall time | 2.028 s | 1.385 s |
| Peak query memory | 508,369,849 bytes | 355,358,543 bytes |
| Query OS write | 12,288 bytes | 0 bytes |
| Business rows/bytes written | 0 / 0 | 0 / 0 |

The host-global `iostat` transfer delta was 1,023.91 MB before and 2,168.20 MB
after. That counter combines all disk traffic from every process and does not
separate writes, so it cannot attribute the higher post value to ClickHouse.
The scoped evidence is the zero query OS write, zero growth in the four
disabled logs, and unchanged old-bind tree.

The old bind retained the same 36,340-entry path/type/size/mtime fingerprint
before and after a persisted write, container restart, 104-second log window,
and representative queries:
`e534801126127d2244f61a20533d4545feb7ff404894a24056f22d280cd372bd`.
No file had a new mtime. Direct recursive macOS watchers still received batches
of non-existent historic `tmp_merge` and `tmp_insert` paths. This is consistent
with the already-abnormal `fseventsd` replaying its backlog, not a live
ClickHouse write: no running container mounts the old path, sampled paths do
not exist, and three subsequent tree fingerprints are identical. Restarting
macOS or the daemon is the remaining host recovery action.

Post-migration verification passed:

- 421 Phase 4 and local-storage Python tests;
- `tsc --noEmit`;
- Next.js 16.2.11 production build;
- Compose render and `git diff --check`;
- normal query, test-owned write, restart persistence, health, mount, and
  resource-policy checks.

The old bind, `waje-bi-clickhouse-data`, and
`waje-bi-clickhouse-data-v2` are preserved. An actual rollback drill was
deferred to avoid a second service interruption after acceptance. The
acknowledgement gate, exact rollback command, old-bind fingerprint, and v3
reattach recovery commands were verified and saved in the evidence package.
