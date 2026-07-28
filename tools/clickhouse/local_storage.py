#!/usr/bin/env python3
"""Audit, migrate, verify, and roll back the local ClickHouse data mount.

All commands are read-only unless both --execute and --acknowledge-downtime are
provided to migrate/rollback, or --write-probe is provided to verify.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.clickhouse.yaml"
CONTAINER = "waje-bi-clickhouse"
DATA_TARGET = "/var/lib/clickhouse"
BIND_SOURCE = ROOT / "data/clickhouse"
VOLUME = "waje-bi-clickhouse-data-v3"
IMAGE = (
    "clickhouse/clickhouse-server:24.8-alpine@"
    "sha256:b002e56ed5c16e224c312527f6fcba7e77216fec5d7a88a7828f59efc614feb5"
)
SYSTEM_LOG_TABLES = (
    "asynchronous_metric_log",
    "error_log",
    "metric_log",
    "part_log",
    "processors_profile_log",
    "query_log",
    "text_log",
    "trace_log",
)
REPRESENTATIVE_QUERY = """
SELECT
    count() AS rows,
    uniqExact(user_id) AS users,
    round(sum(paid_amount_ngn), 2) AS paid_amount_ngn
FROM waje_bi.paid_order_success_clean_20240101_20260704_v2
""".strip()


class OpsError(RuntimeError):
    pass


@dataclass(frozen=True)
class MountState:
    type: str
    source: str
    destination: str
    name: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "type": self.type,
            "source": self.source,
            "destination": self.destination,
            "name": self.name,
        }


def run(
    command: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=str(cwd),
        text=True,
        capture_output=capture,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OpsError(
            "command failed ({}): {}\n{}".format(
                result.returncode,
                " ".join(shlex.quote(part) for part in command),
                detail,
            )
        )
    return result


def docker_inspect(container: str = CONTAINER) -> Dict[str, Any]:
    result = run(["docker", "inspect", container])
    payload = json.loads(result.stdout)
    if len(payload) != 1:
        raise OpsError("expected one Docker inspect result for {}".format(container))
    return payload[0]


def container_exists(container: str = CONTAINER) -> bool:
    return (
        run(
            ["docker", "container", "inspect", container],
            check=False,
        ).returncode
        == 0
    )


def mount_state(inspect: Dict[str, Any]) -> MountState:
    matches = [
        item
        for item in inspect.get("Mounts", [])
        if item.get("Destination") == DATA_TARGET
    ]
    if len(matches) != 1:
        raise OpsError(
            "expected exactly one mount for {}, found {}".format(
                DATA_TARGET, len(matches)
            )
        )
    item = matches[0]
    return MountState(
        type=str(item.get("Type", "")),
        source=str(item.get("Source", "")),
        destination=str(item.get("Destination", "")),
        name=str(item.get("Name", "")),
    )


def ch(query: str, *, fmt: str = "JSONEachRow") -> str:
    command = [
        "docker",
        "exec",
        CONTAINER,
        "clickhouse-client",
        "--query",
        query,
    ]
    if fmt:
        command.extend(["--format", fmt])
    return run(command).stdout.strip()


def ch_rows(query: str) -> List[Dict[str, Any]]:
    payload = ch(query)
    return [json.loads(line) for line in payload.splitlines() if line.strip()]


def quoted_identifier(value: str) -> str:
    return "`{}`".format(value.replace("`", "``"))


def quoted_string(value: str) -> str:
    return "'{}'".format(value.replace("\\", "\\\\").replace("'", "\\'"))


def business_inventory() -> List[Dict[str, Any]]:
    tables = ch_rows(
        """
        SELECT database, name AS table, engine, total_bytes
        FROM system.tables
        WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')
        ORDER BY database, table
        """
    )
    part_rows = ch_rows(
        """
        SELECT
            database,
            table,
            count() AS active_parts,
            sum(rows) AS rows,
            sum(bytes_on_disk) AS bytes_on_disk
        FROM system.parts
        WHERE active
          AND database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')
        GROUP BY database, table
        ORDER BY database, table
        """
    )
    part_map = {
        (str(row["database"]), str(row["table"])): row for row in part_rows
    }
    inventory: List[Dict[str, Any]] = []
    for table in tables:
        database = str(table["database"])
        name = str(table["table"])
        count = int(
            ch(
                "SELECT count() FROM {}.{}".format(
                    quoted_identifier(database), quoted_identifier(name)
                ),
                fmt="TSVRaw",
            )
        )
        parts = part_map.get((database, name), {})
        inventory.append(
            {
                "database": database,
                "table": name,
                "engine": str(table["engine"]),
                "rows": count,
                "active_parts": int(parts.get("active_parts", 0)),
                "bytes_on_disk": int(
                    parts.get("bytes_on_disk", table.get("total_bytes") or 0)
                ),
            }
        )
    return inventory


def system_log_inventory() -> List[Dict[str, Any]]:
    names = ", ".join("'{}'".format(name) for name in SYSTEM_LOG_TABLES)
    return ch_rows(
        """
        SELECT
            table,
            sum(rows) AS rows,
            sum(bytes_on_disk) AS bytes_on_disk,
            count() AS active_parts
        FROM system.parts
        WHERE active AND database = 'system' AND table IN ({})
        GROUP BY table
        ORDER BY table
        """.format(
            names
        )
    )


def selected_inspect(raw: Dict[str, Any]) -> Dict[str, Any]:
    config = raw.get("Config", {})
    host = raw.get("HostConfig", {})
    return {
        "id": raw.get("Id"),
        "name": raw.get("Name"),
        "created": raw.get("Created"),
        "image_id": raw.get("Image"),
        "state": raw.get("State"),
        "config": {
            "image": config.get("Image"),
            "entrypoint": config.get("Entrypoint"),
            "cmd": config.get("Cmd"),
            "environment_names": sorted(
                item.split("=", 1)[0] for item in config.get("Env", [])
            ),
            "healthcheck": config.get("Healthcheck"),
        },
        "host_config": {
            "port_bindings": host.get("PortBindings"),
            "restart_policy": host.get("RestartPolicy"),
            "memory": host.get("Memory"),
            "nano_cpus": host.get("NanoCpus"),
            "log_config": host.get("LogConfig"),
        },
        "mounts": raw.get("Mounts"),
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def write_tsv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    materialized = list(rows)
    if not materialized:
        path.write_text("")
        return
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        delimiter="\t",
        fieldnames=list(materialized[0].keys()),
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(materialized)
    path.write_text(buffer.getvalue())


def default_output(prefix: str) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    return (
        ROOT
        / "output"
        / "clickhouse-local-optimization"
        / "{}-{}".format(prefix, stamp)
    )


def capture_config_files(output: Path) -> None:
    target = output / "clickhouse-config"
    target.mkdir(parents=True, exist_ok=True)
    config_root = Path("/etc/clickhouse-server")
    sources = [
        "/etc/clickhouse-server/config.xml",
        "/etc/clickhouse-server/users.xml",
    ]
    for directory in (
        "/etc/clickhouse-server/config.d",
        "/etc/clickhouse-server/users.d",
    ):
        payload = run(
            [
                "docker",
                "exec",
                CONTAINER,
                "find",
                directory,
                "-maxdepth",
                "1",
                "-type",
                "f",
                "-print",
            ]
        ).stdout
        sources.extend(line for line in payload.splitlines() if line.strip())
    for source in sources:
        relative = Path(source).relative_to(config_root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            run(
                [
                    "docker",
                    "exec",
                    CONTAINER,
                    "sed",
                    "-n",
                    "1,$p",
                    source,
                ]
            ).stdout
        )


def capture_baseline(output: Path) -> Dict[str, Any]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    inspect = docker_inspect()
    mount = mount_state(inspect)
    if not inspect.get("State", {}).get("Running"):
        raise OpsError("{} must be running for an audit".format(CONTAINER))

    health = ch_rows(
        """
        SELECT
            version() AS version,
            uptime() AS uptime_seconds,
            timezone() AS timezone,
            now64(3) AS captured_at
        """
    )[0]
    databases = ch_rows(
        "SELECT name, engine FROM system.databases ORDER BY name"
    )
    tables = business_inventory()
    logs = system_log_inventory()
    log_definitions = ch_rows(
        """
        SELECT
            name AS table,
            engine,
            total_rows,
            total_bytes,
            create_table_query
        FROM system.tables
        WHERE database = 'system'
          AND name IN (
            'asynchronous_metric_log',
            'error_log',
            'metric_log',
            'part_log',
            'processors_profile_log',
            'query_log',
            'text_log',
            'trace_log'
          )
        ORDER BY table
        """
    )
    merges = ch_rows(
        """
        SELECT database, table, elapsed, progress, num_parts, result_part_name
        FROM system.merges
        ORDER BY elapsed DESC
        """
    )
    mutations = ch_rows(
        """
        SELECT
            database,
            table,
            mutation_id,
            command,
            create_time,
            parts_to_do,
            is_done,
            latest_fail_reason
        FROM system.mutations
        ORDER BY create_time DESC
        """
    )
    server_settings = ch_rows(
        """
        SELECT name, value
        FROM system.server_settings
        WHERE name IN (
            'max_connections',
            'max_concurrent_queries',
            'max_server_memory_usage',
            'max_server_memory_usage_to_ram_ratio',
            'max_thread_pool_size',
            'max_thread_pool_free_size',
            'max_io_thread_pool_size',
            'background_pool_size',
            'background_schedule_pool_size',
            'max_parts_cleaning_thread_pool_size'
        )
        ORDER BY name
        """
    )
    profile_settings = ch_rows(
        """
        SELECT name, value
        FROM system.settings
        WHERE name IN (
            'max_threads',
            'max_memory_usage',
            'log_queries',
            'log_queries_min_type',
            'log_query_threads',
            'log_processors_profiles',
            'query_profiler_real_time_period_ns',
            'query_profiler_cpu_time_period_ns',
            'memory_profiler_step',
            'memory_profiler_sample_probability'
        )
        ORDER BY name
        """
    )

    compose = run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config"]
    ).stdout
    (output / "compose-rendered.yaml").write_text(compose)
    write_json(output / "docker-inspect.redacted.json", selected_inspect(inspect))
    write_json(output / "docker-mount.json", mount.as_dict())
    write_json(output / "databases.json", databases)
    write_json(output / "business-tables.json", tables)
    write_tsv(output / "business-tables.tsv", tables)
    write_json(output / "system-logs.json", logs)
    write_tsv(output / "system-logs.tsv", logs)
    write_json(output / "system-log-definitions.json", log_definitions)
    write_json(output / "merges.json", merges)
    write_json(output / "mutations.json", mutations)
    write_json(output / "server-settings.json", server_settings)
    write_json(output / "profile-settings.json", profile_settings)
    (output / "docker-stats.txt").write_text(
        run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                CONTAINER,
            ]
        ).stdout
    )
    (output / "host-data-size.txt").write_text(
        run(["du", "-sk", str(BIND_SOURCE)], check=False).stdout
    )
    (output / "spotlight-status.txt").write_text(
        run(["mdutil", "-s", str(BIND_SOURCE)], check=False).stdout
    )
    capture_config_files(output)

    baseline = {
        "schema_version": 1,
        "captured_at": datetime.now().astimezone().isoformat(),
        "root": str(ROOT),
        "container": CONTAINER,
        "image": inspect.get("Config", {}).get("Image"),
        "image_id": inspect.get("Image"),
        "health": health,
        "mount": mount.as_dict(),
        "databases": databases,
        "business_tables": tables,
        "system_logs": logs,
        "system_log_definitions": log_definitions,
        "merges": merges,
        "mutations": mutations,
        "server_settings": server_settings,
        "profile_settings": profile_settings,
    }
    write_json(output / "baseline.json", baseline)
    write_migration_commands(output)
    return baseline


def write_migration_commands(output: Path) -> None:
    script = ROOT / "tools/clickhouse/local_storage.py"
    lines = [
        "# Generated local ClickHouse commands",
        "",
        "These commands are inert until the user approves a downtime window.",
        "",
        "## Refresh audit",
        "",
        "```sh",
        "{} {} audit --output {}".format(
            shlex.quote(sys.executable), shlex.quote(str(script)), shlex.quote(str(output))
        ),
        "```",
        "",
        "## Dry-run plan",
        "",
        "```sh",
        "{} {} plan".format(
            shlex.quote(sys.executable), shlex.quote(str(script))
        ),
        "```",
        "",
        "## Migrate after explicit downtime approval",
        "",
        "```sh",
        "{} {} migrate --execute --acknowledge-downtime --artifact {}".format(
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            shlex.quote(str(output)),
        ),
        "```",
        "",
        "## Comparable idle and query measurements",
        "",
        "```sh",
        "{} {} rate --seconds 104 --output {}".format(
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            shlex.quote(str(output / "idle-rate")),
        ),
        "{} {} benchmark --output {}".format(
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            shlex.quote(str(output / "representative-query")),
        ),
        "```",
        "",
        "## Verify",
        "",
        "```sh",
        "{} {} verify --baseline {} --output {} --require-volume".format(
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            shlex.quote(str(output / "baseline.json")),
            shlex.quote(str(output / "post-migration")),
        ),
        "```",
        "",
        "## Roll back during the acceptance window",
        "",
        "```sh",
        "{} {} rollback --execute --acknowledge-downtime".format(
            shlex.quote(sys.executable), shlex.quote(str(script))
        ),
        "```",
        "",
        "Rollback reattaches the preserved bind directory. It does not delete the "
        "named volume.",
        "",
    ]
    (output / "commands.md").write_text("\n".join(lines))


def rate_snapshot() -> Dict[str, Any]:
    stats = run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            CONTAINER,
        ]
    ).stdout.strip()
    return {
        "captured_at": datetime.now().astimezone().isoformat(),
        "logs": system_log_inventory(),
        "docker_stats": json.loads(stats),
    }


def rate_delta(before: Dict[str, Any], after: Dict[str, Any], elapsed: float) -> Dict[str, Any]:
    before_map = {row["table"]: row for row in before["logs"]}
    after_map = {row["table"]: row for row in after["logs"]}
    deltas = []
    for table in sorted(set(before_map) | set(after_map)):
        old = before_map.get(table, {})
        new = after_map.get(table, {})
        rows = int(new.get("rows", 0)) - int(old.get("rows", 0))
        bytes_delta = int(new.get("bytes_on_disk", 0)) - int(
            old.get("bytes_on_disk", 0)
        )
        deltas.append(
            {
                "table": table,
                "rows_delta": rows,
                "rows_per_second": rows / elapsed,
                "bytes_on_disk_delta": bytes_delta,
                "bytes_on_disk_per_second": bytes_delta / elapsed,
            }
        )
    return {
        "elapsed_seconds": elapsed,
        "before": before,
        "after": after,
        "deltas": deltas,
    }


def command_benchmark(args: argparse.Namespace) -> int:
    output = (args.output or default_output("benchmark")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    query_id = "waje-local-storage-benchmark-{}-{}".format(
        int(time.time()), os.getpid()
    )
    before = rate_snapshot()
    host_io_before = run(["iostat", "-Id", "disk0"], check=False).stdout
    started = time.monotonic()
    query = run(
        [
            "docker",
            "exec",
            CONTAINER,
            "clickhouse-client",
            "--query_id",
            query_id,
            "--query",
            REPRESENTATIVE_QUERY,
            "--format",
            "JSONEachRow",
        ]
    ).stdout.strip()
    elapsed = time.monotonic() - started
    ch("SYSTEM FLUSH LOGS", fmt="")
    after = rate_snapshot()
    host_io_after = run(["iostat", "-Id", "disk0"], check=False).stdout
    query_log = ch_rows(
        """
        SELECT
            query_id,
            query_duration_ms,
            read_rows,
            read_bytes,
            written_rows,
            written_bytes,
            memory_usage,
            ProfileEvents['OSReadBytes'] AS os_read_bytes,
            ProfileEvents['OSWriteBytes'] AS os_write_bytes
        FROM system.query_log
        WHERE query_id = {}
          AND type = 'QueryFinish'
        ORDER BY event_time_microseconds DESC
        LIMIT 1
        """.format(
            quoted_string(query_id)
        )
    )
    report = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "query_id": query_id,
        "query": REPRESENTATIVE_QUERY,
        "result": json.loads(query),
        "wall_seconds": elapsed,
        "query_log": query_log[0] if query_log else None,
        "system_log_and_container_delta": rate_delta(before, after, elapsed),
        "host_iostat_before": host_io_before,
        "host_iostat_after": host_io_after,
    }
    (output / "representative-query.sql").write_text(REPRESENTATIVE_QUERY + "\n")
    write_json(output / "representative-query.json", report)
    print(
        json.dumps(
            {
                "status": "captured",
                "output": str(output),
                "wall_seconds": elapsed,
                "query_log": report["query_log"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def wait_ready(timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        result = run(
            [
                "docker",
                "exec",
                CONTAINER,
                "clickhouse-client",
                "--query",
                "SELECT 1",
            ],
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = result.stderr.strip() or result.stdout.strip()
        time.sleep(2)
    raise OpsError(
        "{} did not become queryable within {}s: {}".format(
            CONTAINER, timeout_seconds, last_error
        )
    )


def compose_command(*args: str) -> List[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *args]


def volume_exists() -> bool:
    return (
        run(["docker", "volume", "inspect", VOLUME], check=False).returncode == 0
    )


def helper_command(script: str, *, bind_source: Path = BIND_SOURCE) -> str:
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            "type=bind,src={},dst=/source,readonly".format(bind_source),
            "--mount",
            "type=volume,src={},dst=/target".format(VOLUME),
            "--entrypoint",
            "/bin/sh",
            IMAGE,
            "-ec",
            script,
        ]
    )
    return result.stdout.strip()


MANIFEST_SCRIPT = r"""
digest_structure() {
    root="$1"
    (
        cd "$root"
        find . -xdev -print0 |
            sort -z |
            xargs -0 stat -c '%n|%F'
    ) | sha256sum | awk '{print $1}'
}
digest_nonlink_modes() {
    root="$1"
    (
        cd "$root"
        find . -xdev ! -type l -print0 |
            sort -z |
            xargs -0 stat -c '%n|%F|%a'
    ) | sha256sum | awk '{print $1}'
}
digest_file_mtimes() {
    root="$1"
    (
        cd "$root"
        find . -xdev -type f -print0 |
            sort -z |
            xargs -0 stat -c '%n|%Y'
    ) | sha256sum | awk '{print $1}'
}
digest_files() {
    (
        cd "$1"
        find . -xdev -type f -print0 |
            sort -z |
            xargs -0 sha256sum
    ) | sha256sum | awk '{print $1}'
}
digest_links() {
    (
        cd "$1"
        find . -xdev -type l -exec sh -c \
            'for path; do printf "%s|%s\n" "$path" "$(readlink "$path")"; done' \
            sh {} + |
            sort
    ) | sha256sum | awk '{print $1}'
}
runtime_uid="$(id -u clickhouse)"
runtime_gid="$(id -g clickhouse)"
target_owner_mismatches="$(
    find /target -xdev ! -type l -print0 |
        xargs -0 stat -c '%u|%g' |
        awk -F '|' -v uid="$runtime_uid" -v gid="$runtime_gid" \
            '$1 != uid || $2 != gid { count++ } END { print count + 0 }'
)"
target_unreadable_files="$(
    clickhouse su "$runtime_uid:$runtime_gid" sh -ec \
        'find /target -xdev -type f -exec sh -c '"'"'
            for path do
                test -r "$path" || printf "%s\n" "$path"
            done
        '"'"' sh {} + | wc -l'
)"
target_unsearchable_directories="$(
    clickhouse su "$runtime_uid:$runtime_gid" sh -ec \
        'find /target -xdev -type d -exec sh -c '"'"'
            for path do
                test -x "$path" || printf "%s\n" "$path"
            done
        '"'"' sh {} + | wc -l'
)"
printf 'source_structure=%s\n' "$(digest_structure /source)"
printf 'target_structure=%s\n' "$(digest_structure /target)"
printf 'source_nonlink_modes=%s\n' "$(digest_nonlink_modes /source)"
printf 'target_nonlink_modes=%s\n' "$(digest_nonlink_modes /target)"
printf 'source_file_mtimes=%s\n' "$(digest_file_mtimes /source)"
printf 'target_file_mtimes=%s\n' "$(digest_file_mtimes /target)"
printf 'source_files=%s\n' "$(digest_files /source)"
printf 'target_files=%s\n' "$(digest_files /target)"
printf 'source_links=%s\n' "$(digest_links /source)"
printf 'target_links=%s\n' "$(digest_links /target)"
printf 'runtime_uid=%s\n' "$runtime_uid"
printf 'runtime_gid=%s\n' "$runtime_gid"
printf 'target_owner_mismatches=%s\n' "$target_owner_mismatches"
printf 'target_unreadable_files=%s\n' "$target_unreadable_files"
printf 'target_unsearchable_directories=%s\n' \
    "$target_unsearchable_directories"
"""


def parse_key_values(payload: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in payload.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def validate_copy_manifest(values: Mapping[str, str]) -> Dict[str, Any]:
    required = {
        "source_structure",
        "target_structure",
        "source_nonlink_modes",
        "target_nonlink_modes",
        "source_file_mtimes",
        "target_file_mtimes",
        "source_files",
        "target_files",
        "source_links",
        "target_links",
        "runtime_uid",
        "runtime_gid",
        "target_owner_mismatches",
        "target_unreadable_files",
        "target_unsearchable_directories",
    }
    if set(values) != required:
        raise OpsError("unexpected manifest output: {}".format(values))
    digest_contracts = (
        ("structure", "path/type"),
        ("nonlink_modes", "non-link mode"),
        ("file_mtimes", "file mtime"),
        ("files", "file-content"),
        ("links", "symbolic-link target"),
    )
    for field, label in digest_contracts:
        if values["source_{}".format(field)] != values["target_{}".format(field)]:
            raise OpsError("source/target {} digest mismatch".format(label))
    for field, label in (
        ("target_owner_mismatches", "runtime-owner mismatch"),
        ("target_unreadable_files", "unreadable file"),
        ("target_unsearchable_directories", "unsearchable directory"),
    ):
        try:
            count = int(values[field])
        except ValueError as error:
            raise OpsError("invalid {} count: {}".format(label, values[field])) from error
        if count:
            raise OpsError("target has {} {} entries".format(count, label))
    return {
        "schema_version": 2,
        "strong_integrity": {
            "path_type_sha256": values["source_structure"],
            "nonlink_mode_sha256": values["source_nonlink_modes"],
            "file_mtime_sha256": values["source_file_mtimes"],
            "file_content_sha256": values["source_files"],
            "symbolic_link_target_sha256": values["source_links"],
        },
        "target_runtime_access": {
            "uid": int(values["runtime_uid"]),
            "gid": int(values["runtime_gid"]),
            "owner_mismatches": int(values["target_owner_mismatches"]),
            "unreadable_files": int(values["target_unreadable_files"]),
            "unsearchable_directories": int(
                values["target_unsearchable_directories"]
            ),
        },
        "cross_filesystem_policy": {
            "source_uid_gid_compared_per_path": False,
            "symlink_mode_compared": False,
            "reason": (
                "Docker Desktop bind ownership is caller-view dependent and "
                "symlink mode is not portable; the Linux volume is normalized "
                "to the image runtime user before access checks"
            ),
        },
    }


def compare_volume_to_bind() -> Dict[str, Any]:
    return validate_copy_manifest(parse_key_values(helper_command(MANIFEST_SCRIPT)))


def copy_bind_to_volume() -> Dict[str, Any]:
    output = helper_command(
        r"""
if find /target -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    exit 42
fi
cp -a /source/. /target/
runtime_owner="$(id -u clickhouse):$(id -g clickhouse)"
source_mode="$(stat -c '%a' /source)"
chown -R "$runtime_owner" /target
chmod "$source_mode" /target
touch -r /source /target
sync
"""
    )
    if output:
        print(output)
    return compare_volume_to_bind()


def volume_is_empty() -> bool:
    result = helper_command(
        "if find /target -mindepth 1 -maxdepth 1 -print -quit | grep -q .; "
        "then printf nonempty; else printf empty; fi"
    )
    return result == "empty"


def recreate_bind_container() -> None:
    if container_exists():
        inspect = docker_inspect()
        if inspect.get("State", {}).get("Running"):
            run(["docker", "stop", "--timeout", "120", CONTAINER], capture=False)
        run(["docker", "rm", CONTAINER], capture=False)
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            "18123:8123",
            "-p",
            "19000:9000",
            "--mount",
            "type=bind,src={},dst={}".format(BIND_SOURCE, DATA_TARGET),
            IMAGE,
        ],
        capture=False,
    )
    wait_ready()


def require_downtime_ack(args: argparse.Namespace) -> None:
    if not args.execute or not args.acknowledge_downtime:
        raise OpsError(
            "mutation blocked: pass both --execute and --acknowledge-downtime "
            "after the user confirms the stop window"
        )


def load_baseline(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 1:
        raise OpsError("unsupported baseline schema in {}".format(path))
    return payload


def compare_tables(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]]
) -> Dict[str, Any]:
    old = {(row["database"], row["table"]): row for row in before}
    new = {(row["database"], row["table"]): row for row in after}
    missing = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    row_mismatches = []
    storage_deltas = []
    for key in sorted(set(old) & set(new)):
        if int(old[key]["rows"]) != int(new[key]["rows"]):
            row_mismatches.append(
                {
                    "database": key[0],
                    "table": key[1],
                    "before": int(old[key]["rows"]),
                    "after": int(new[key]["rows"]),
                }
            )
        storage_deltas.append(
            {
                "database": key[0],
                "table": key[1],
                "active_parts_before": int(old[key]["active_parts"]),
                "active_parts_after": int(new[key]["active_parts"]),
                "bytes_on_disk_before": int(old[key]["bytes_on_disk"]),
                "bytes_on_disk_after": int(new[key]["bytes_on_disk"]),
            }
        )
    return {
        "missing_tables": missing,
        "added_tables": added,
        "row_mismatches": row_mismatches,
        "storage_deltas": storage_deltas,
        "passed": not missing and not added and not row_mismatches,
    }


def run_write_probe(restart: bool) -> Dict[str, Any]:
    suffix = "{}_{}".format(int(time.time()), os.getpid())
    table = "__waje_local_storage_probe_{}".format(suffix)
    token = "probe-{}".format(suffix)
    full_table = "default.{}".format(quoted_identifier(table))
    ch(
        "CREATE TABLE {} (token String) ENGINE = MergeTree ORDER BY token".format(
            full_table
        ),
        fmt="",
    )
    try:
        ch(
            "INSERT INTO {} VALUES ({})".format(
                full_table, quoted_string(token)
            ),
            fmt="",
        )
        if restart:
            run(["docker", "restart", "--timeout", "120", CONTAINER], capture=False)
            wait_ready()
        observed = ch(
            "SELECT token FROM {} WHERE token = {}".format(
                full_table, quoted_string(token)
            ),
            fmt="TSVRaw",
        )
        if observed != token:
            raise OpsError("write probe token did not persist")
        return {"table": "default.{}".format(table), "token": token, "restart": restart}
    finally:
        ch("DROP TABLE IF EXISTS {}".format(full_table), fmt="")


def command_audit(args: argparse.Namespace) -> int:
    output = args.output or default_output("pre-migration")
    baseline = capture_baseline(output)
    print(
        json.dumps(
            {
                "status": "captured",
                "output": str(output.resolve()),
                "mount": baseline["mount"],
                "business_table_count": len(baseline["business_tables"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_rate(args: argparse.Namespace) -> int:
    output = (args.output or default_output("rate")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    before = rate_snapshot()
    started = time.monotonic()
    time.sleep(args.seconds)
    elapsed = time.monotonic() - started
    after = rate_snapshot()
    payload = rate_delta(before, after, elapsed)
    write_json(output / "system-log-rate.json", payload)
    print(json.dumps({"status": "captured", "output": str(output), **payload}))
    return 0


def command_plan(args: argparse.Namespace) -> int:
    inspect = docker_inspect()
    mount = mount_state(inspect)
    plan = {
        "status": "dry_run",
        "container_running": bool(inspect.get("State", {}).get("Running")),
        "current_mount": mount.as_dict(),
        "target_mount": {
            "type": "volume",
            "name": VOLUME,
            "destination": DATA_TARGET,
        },
        "target_volume_exists": volume_exists(),
        "steps": [
            "capture a fresh baseline while ClickHouse is healthy",
            "docker stop --timeout 120 {}".format(CONTAINER),
            "create {} only when absent".format(VOLUME),
            (
                "copy bind data with mode/timestamps preserved and normalize "
                "the Linux volume to the image runtime UID/GID"
            ),
            (
                "compare path/type, non-link mode, file mtime, symbolic-link "
                "target, and file-content SHA-256 digests"
            ),
            "remove only the stopped container object",
            "docker compose up with the named volume",
            "compare every business table row count and run health checks",
            "keep {} unchanged as rollback data".format(BIND_SOURCE),
        ],
        "rollback": [
            "stop the volume-backed container normally",
            "remove only that stopped container object",
            "recreate {} with the preserved bind mount".format(CONTAINER),
            "leave {} untouched".format(VOLUME),
        ],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    baseline = load_baseline(args.baseline.resolve())
    output = (args.output or default_output("post-migration")).resolve()
    inspect = docker_inspect()
    mount = mount_state(inspect)
    if args.require_volume and not (
        mount.type == "volume" and mount.name == VOLUME
    ):
        raise OpsError(
            "expected named volume {}, found {}".format(VOLUME, mount.as_dict())
        )
    run(compose_command("config"))
    wait_ready()
    probe = None
    if args.write_probe:
        probe = run_write_probe(args.restart_probe)
    after = capture_baseline(output)
    comparison = compare_tables(
        baseline["business_tables"], after["business_tables"]
    )
    health_status = inspect.get("State", {}).get("Health", {}).get("Status")
    report = {
        "status": "passed" if comparison["passed"] else "failed",
        "mount": mount.as_dict(),
        "docker_health_status": health_status,
        "table_comparison": comparison,
        "write_probe": probe,
        "output": str(output),
    }
    write_json(output / "verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not comparison["passed"]:
        raise OpsError("business table verification failed")
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    if not args.execute:
        return command_plan(args)
    require_downtime_ack(args)
    artifact = (args.artifact or default_output("migration")).resolve()
    artifact.mkdir(parents=True, exist_ok=True)
    inspect = docker_inspect()
    current = mount_state(inspect)
    if current.type == "volume" and current.name == VOLUME:
        print(
            json.dumps(
                {"status": "already_migrated", "mount": current.as_dict()},
                ensure_ascii=False,
            )
        )
        return 0
    if current.type != "bind" or Path(current.source).resolve() != BIND_SOURCE.resolve():
        raise OpsError("unexpected current data mount: {}".format(current.as_dict()))
    baseline = capture_baseline(artifact)
    run(compose_command("config"))
    stopped_original_exists = True
    try:
        run(["docker", "stop", "--timeout", "120", CONTAINER], capture=False)
        stopped = docker_inspect()
        if stopped.get("State", {}).get("Running"):
            raise OpsError("{} did not stop cleanly".format(CONTAINER))
        if not volume_exists():
            run(["docker", "volume", "create", VOLUME], capture=False)
        if volume_is_empty():
            manifest = copy_bind_to_volume()
        else:
            manifest = compare_volume_to_bind()
        write_json(artifact / "copy-manifest.json", manifest)
        run(["docker", "rm", CONTAINER], capture=False)
        stopped_original_exists = False
        run(compose_command("up", "-d", "clickhouse"), capture=False)
        wait_ready()
        verify_args = argparse.Namespace(
            baseline=artifact / "baseline.json",
            output=artifact / "post-migration",
            require_volume=True,
            write_probe=False,
            restart_probe=False,
        )
        command_verify(verify_args)
        print(
            json.dumps(
                {
                    "status": "migrated",
                    "artifact": str(artifact),
                    "rollback_bind": str(BIND_SOURCE),
                    "named_volume": VOLUME,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception:
        if stopped_original_exists and container_exists():
            state = docker_inspect().get("State", {})
            if not state.get("Running"):
                run(["docker", "start", CONTAINER], check=False, capture=False)
                wait_ready()
        elif not stopped_original_exists:
            recreate_bind_container()
        raise


def command_rollback(args: argparse.Namespace) -> int:
    require_downtime_ack(args)
    inspect = docker_inspect()
    current = mount_state(inspect)
    if current.type == "bind" and Path(current.source).resolve() == BIND_SOURCE.resolve():
        print(
            json.dumps(
                {"status": "already_rolled_back", "mount": current.as_dict()},
                ensure_ascii=False,
            )
        )
        return 0
    if current.type != "volume" or current.name != VOLUME:
        raise OpsError("unexpected current data mount: {}".format(current.as_dict()))
    recreate_bind_container()
    mount = mount_state(docker_inspect())
    print(
        json.dumps(
            {
                "status": "rolled_back",
                "mount": mount.as_dict(),
                "preserved_named_volume": VOLUME,
                "warning": (
                    "the bind copy contains the migration-point state; writes made "
                    "after cutover remain only in the named volume"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="capture a read-only baseline")
    audit.add_argument("--output", type=Path)
    audit.set_defaults(handler=command_audit)

    rate = commands.add_parser("rate", help="measure system-log growth")
    rate.add_argument("--seconds", type=int, default=60)
    rate.add_argument("--output", type=Path)
    rate.set_defaults(handler=command_rate)

    benchmark = commands.add_parser(
        "benchmark", help="run the fixed read-only 41M-row representative query"
    )
    benchmark.add_argument("--output", type=Path)
    benchmark.set_defaults(handler=command_benchmark)

    plan = commands.add_parser("plan", help="show the non-mutating migration plan")
    plan.set_defaults(handler=command_plan)

    migrate = commands.add_parser("migrate", help="copy bind data and switch Compose")
    migrate.add_argument("--execute", action="store_true")
    migrate.add_argument("--acknowledge-downtime", action="store_true")
    migrate.add_argument("--artifact", type=Path)
    migrate.set_defaults(handler=command_migrate)

    verify = commands.add_parser("verify", help="verify mount, tables, and health")
    verify.add_argument("--baseline", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    verify.add_argument("--require-volume", action="store_true")
    verify.add_argument("--write-probe", action="store_true")
    verify.add_argument("--restart-probe", action="store_true")
    verify.set_defaults(handler=command_verify)

    rollback = commands.add_parser(
        "rollback", help="reattach the preserved bind directory"
    )
    rollback.add_argument("--execute", action="store_true")
    rollback.add_argument("--acknowledge-downtime", action="store_true")
    rollback.set_defaults(handler=command_rollback)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "restart_probe", False) and not args.write_probe:
        parser.error("--restart-probe requires --write-probe")
    try:
        return int(args.handler(args))
    except OpsError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
