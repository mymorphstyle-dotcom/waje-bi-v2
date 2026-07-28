# Local ClickHouse storage and macOS pressure runbook

## Scope and authority

This runbook owns the local-development storage and operating contract for
`waje-bi-clickhouse`. It does not change BI semantics, dataset releases, SQL
safety, evidence, verifier, customer projection, or the PostgreSQL persistence
boundary.

The current image is pinned to ClickHouse `24.8.14.39` by digest. The server
configuration uses the documented `config.d` merge mechanism and
`replace`/`remove` attributes. See the
[ClickHouse configuration file reference](https://clickhouse.com/docs/operations/configuration-files).

The repository files are:

- `compose.clickhouse.yaml`
- `ops/clickhouse/config.d/90-waje-development.xml`
- `ops/clickhouse/users.d/90-waje-development.xml`
- `tools/clickhouse/local_storage.py`

The migration command is locked until a user-approved downtime window. Do not
run `migrate --execute` or `rollback --execute` before that approval.

## Current and target mounts

| Item | Current | Target |
|---|---|---|
| Container | `waje-bi-clickhouse` | same |
| Image | `clickhouse/clickhouse-server:24.8-alpine` | same 24.8.14.39 image, pinned by digest |
| Data target | `/var/lib/clickhouse` | same |
| Mount | bind: `/Users/luka/work/waje-bi-v2/data/clickhouse` | named volume: `waje-bi-clickhouse-data-v3` |
| HTTP/native ports | `18123` / `19000` | same |
| Config mounts | none | two read-only repository XML files |
| Healthcheck | none | `SELECT 1`, 10-second interval |
| Container limits | none | 12 CPUs, 18 GiB |
| Stop grace | Docker default | 120 seconds |

Application source remains on the macOS filesystem. PostgreSQL remains on its
current local bind mount and keeps its existing persistence responsibility.

A named volume removes the ClickHouse MergeTree part tree from the repository
path seen by FSEvents, Spotlight, editors, and file watchers. ClickHouse still
writes to Docker Desktop's internal virtual disk on the same physical SSD.
The migration tool creates the named volume; Compose declares it `external`
and only attaches it. Failed-attempt volumes are never reused or removed
automatically.

## Development system-log policy

The current stock 24.8 configuration writes `metric_log` once per second,
`asynchronous_metric_log` at every asynchronous-metric collection, `text_log`
at `trace`, processor profiles, and query profiler traces. The target policy is:

| Log | Target policy | Retention / flush |
|---|---|---|
| `query_log` | keep query finishes and failures; omit query-start duplicates | 14 days / 30 seconds |
| `part_log` | keep MergeTree part diagnostics | 14 days / 30 seconds |
| `text_log` | keep warning and error messages | 7 days / 30 seconds |
| `error_log` | keep aggregated server errors | 30 days / 30 seconds |
| `trace_log` | disable persistent query profiler traces | disabled |
| `metric_log` | use live `system.metrics` when needed | disabled |
| `asynchronous_metric_log` | use live `system.asynchronous_metrics` when needed | disabled |
| `processors_profile_log` | enable temporarily only for a specific investigation | disabled |

Query CPU/real-time profilers, memory stack sampling, and processor profile
logging are disabled in the default development profile. Existing system-log
parts remain in the copied data. The startup schema check can rename an old
system-log table and create the bounded replacement. This preserves historical
rows without an in-place `ALTER`, mass TTL rewrite, or `OPTIMIZE FINAL`.
The first stage leaves the roughly 1.48 GB of existing system-log parts in
place; it does not reclaim that disk capacity.

To temporarily investigate a performance incident, add a short-lived,
reviewed override in `users.d`; capture the evidence and remove the override
afterward. Do not turn the high-frequency defaults back on permanently.

## Resource policy

The pre-migration seven-day query sample contains 8,860 completed selects:

- memory P50 27.46 MiB, P95 2.25 GiB, P99 2.54 GiB;
- observed maximum 12.49 GiB;
- representative BI queries read 82,469,354 rows at about 2.9 GiB;
- older full data-cleaning attempts reached roughly 17.5 GiB and already
  encountered memory limits.

The repository configuration therefore keeps an 18 GiB container ceiling and
a 16 GiB ClickHouse server ceiling. It limits query concurrency to 32,
`max_threads` to 12, the global thread pool to 2,048 with 128 retained idle
threads, the background schedule pool to 64, and the MergeTree pool to 8.
MergeTree free-entry thresholds are scaled to the resulting 16 merge/mutation
slots so ClickHouse 24.8 passes its startup sanity check.

Keep Docker Desktop memory at 20 GiB through migration acceptance. The existing
12.49 GiB query peak makes a lower global allocation unsafe. The container CPU
cap is 12; if Docker Desktop is manually reduced from 18 CPUs, use 12 CPUs and
rerun the representative benchmark. The repository does not modify Docker
Desktop global settings.

## macOS indexing and watcher exclusions

Add these directories in **System Settings → Siri & Spotlight → Spotlight
Privacy**:

- `/Users/luka/work/waje-bi-v2/data/clickhouse`
- `/Users/luka/work/waje-bi-v2/data/postgres`
- `/Users/luka/work/waje-bi-v2/.next`
- `/Users/luka/work/waje-bi-v2/.turbo`
- `/Users/luka/work/waje-bi-v2/node_modules`

The repository `.vscode/settings.json` excludes the same directory families
from file watching and search. The old ClickHouse bind directory stays in
Spotlight Privacy after migration because it remains the rollback copy.

`fseventsd` recovery is a separate host operation. A process already consuming
abnormal CPU/RSS may require a service restart or a macOS reboot. Judge this
change by path-level event reduction, system-log growth, and comparable write
measurements as well as the daemon's later recovery.

## Read-only preflight

From `/Users/luka/work/waje-bi-v2`:

```sh
python3 tools/clickhouse/local_storage.py plan
python3 tools/clickhouse/local_storage.py audit \
  --output output/clickhouse-local-optimization/pre-migration-$(date +%Y%m%dT%H%M%S%z)
docker compose -f compose.clickhouse.yaml config
```

`audit` stores redacted Docker inspect data, the exact current XML, mount
information, every database and table, exact business row counts, active parts,
bytes on disk, system-log definitions, merges, mutations, settings, health, and
generated migration/rollback commands.

Capture comparable measurements before the stop window:

```sh
python3 tools/clickhouse/local_storage.py rate \
  --seconds 104 \
  --output output/clickhouse-local-optimization/pre-idle-rate
python3 tools/clickhouse/local_storage.py benchmark \
  --output output/clickhouse-local-optimization/pre-query
```

Use the same duration and the same fixed benchmark after cutover.

## Approved downtime procedure

Budget a 30–60 minute window. The exact duration depends on Docker Desktop's
copy and double SHA-256 pass over roughly 16 GiB.

1. Stop WAJE workers, development servers, eval runners, and any manual
   ClickHouse writers. Confirm `system.processes` contains no application query
   and `system.merges` is empty or understood.
2. Capture a fresh audit into a new artifact directory.
3. Run the migration command below. It performs `docker stop --timeout 120`, then
   confirms the container is stopped.
4. The tool creates `waje-bi-clickhouse-data-v3` only when absent.
5. A helper container copies with `cp -a`, preserves non-link modes and file
   timestamps, then normalizes the Linux volume to the image's ClickHouse
   runtime UID/GID.
6. The portable copy contract requires exact path/type, non-link mode, file
   mtime, symbolic-link-target, and per-file-content SHA-256 digests. It also
   requires zero target owner mismatches, unreadable files, or unsearchable
   directories for the ClickHouse runtime user. Per-path source UID/GID and
   symbolic-link mode are recorded as cross-filesystem observations and do not
   gate the copy: Docker Desktop bind ownership is caller-view dependent, and
   symbolic-link mode is not portable.
7. Only after the digests match, the tool removes the stopped container object
   and starts the Compose service.
8. The tool verifies the volume mount, health, database/table set, and every
   business table row count.
9. Keep `/Users/luka/work/waje-bi-v2/data/clickhouse` unchanged.

Command:

```sh
python3 tools/clickhouse/local_storage.py migrate \
  --execute \
  --acknowledge-downtime \
  --artifact /absolute/path/to/fresh-pre-migration-artifact
```

The command is idempotent for an already-migrated mount and for an existing
target volume whose complete portable copy contract matches the stopped bind
source. A non-empty target volume with any strong digest or runtime-access
difference fails closed. The tool never deletes or empties a named volume.

## Post-migration acceptance

Run all of these before restoring normal writers:

```sh
docker compose -f compose.clickhouse.yaml config
docker inspect waje-bi-clickhouse \
  --format '{{range .Mounts}}{{.Type}} {{.Name}} {{.Destination}}{{println}}{{end}}'
python3 tools/clickhouse/local_storage.py verify \
  --baseline /absolute/path/to/pre-migration/baseline.json \
  --output /absolute/path/to/post-migration \
  --require-volume \
  --write-probe \
  --restart-probe
python3 tools/clickhouse/local_storage.py rate \
  --seconds 104 \
  --output /absolute/path/to/post-migration/idle-rate
python3 tools/clickhouse/local_storage.py benchmark \
  --output /absolute/path/to/post-migration/representative-query
```

The write probe creates a unique test-owned MergeTree table, inserts one token,
restarts the container, verifies the token, and drops only that test-owned
table. The business table comparison requires identical table sets and row
counts. Active-part and byte differences are reported for explanation; a
background merge may change those values without changing rows.

Also verify:

- Docker health status is `healthy`;
- the mount at `/var/lib/clickhouse` has `Type=volume` and
  `Name=waje-bi-clickhouse-data-v3`;
- system logs no longer create `trace_log`, `metric_log`,
  `asynchronous_metric_log`, or `processors_profile_log` rows;
- the repository `data/clickhouse` path remains quiet during a representative
  query;
- the fixed query has the same result, comparable read rows, and no business
  writes;
- project tests, type checks, and relevant integration tests pass.

## Rollback

Use rollback during the acceptance window, before accepting new business writes
into the named volume:

```sh
python3 tools/clickhouse/local_storage.py rollback \
  --execute \
  --acknowledge-downtime
```

Rollback stops the volume-backed container normally, removes only that stopped
container object, and recreates `waje-bi-clickhouse` with the preserved bind
directory and original ports. It leaves `waje-bi-clickhouse-data-v3` intact.

After cutover accepts new writes, the old bind contains migration-point data.
Do not use it as a current rollback source until the new writes have been
reconciled or exported.

To restore the accepted v3 volume after a rollback drill, normally stop and
remove only the bind-backed `waje-bi-clickhouse` container object, then run:

```sh
docker compose -f compose.clickhouse.yaml up -d clickhouse
```

This reattaches the preserved external volume. It does not copy, empty, or
delete either storage location.

## Forbidden operations

Do not run:

- `docker system prune`;
- `docker volume prune`;
- `docker compose down -v`;
- any command that deletes `data/clickhouse`;
- any command that deletes an existing named volume;
- an unreviewed `OPTIMIZE FINAL` on a large business table;
- a rebuild without a verified backup and row-count baseline.
