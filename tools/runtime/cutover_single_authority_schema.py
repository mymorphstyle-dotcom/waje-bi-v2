from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "tools/runtime/conversation-runtime.sql"
SINGLE_AUTHORITY_MARKER = "-- Current single-authority workflow slice."
SINGLE_AUTHORITY_MIGRATION_ID = "single-authority-workflow.v23"
SINGLE_AUTHORITY_MIGRATION_DIGEST = (
    "5d63799f71fb49f0c898554573592bb2885059a026f395576de3a04bfa588feb"
)
SOURCE_MIGRATION_ID = "single-authority-workflow.v7"
SOURCE_MIGRATION_DIGEST = (
    "b735fa8fb3d888a3d12be7f335711956e37ba4fc344d294bfbee59a92ac5e3cf"
)
IN_PLACE_SOURCE_MIGRATION_ID = "single-authority-workflow.v17"
IN_PLACE_SOURCE_MIGRATION_DIGEST = (
    "76a80e5f454b02b0a494c9e470a8d837caa19e96823cc1fd68bc0e988e39dd31"
)
IN_PLACE_SOURCE_CONTRACTS = {
    (
        IN_PLACE_SOURCE_MIGRATION_ID,
        IN_PLACE_SOURCE_MIGRATION_DIGEST,
    ): frozenset(),
}
IN_PLACE_BACKFILL_PREDICATES = {
    "conversation_messages": "item_sequence IS NULL",
    "investigation_threads": "latest_item_sequence = 0",
    "dataset_snapshots": "logical_snapshot_id = '' OR load_revision = ''",
    "query_execution_authority": "run_id IS NULL",
    "query_completeness_reports": "run_id IS NULL",
    "capability_binding_authority": "run_id IS NULL",
    "analysis_runs": "run_attempt_id IS NULL OR run_attempt_id = ''",
    "decision_records": "run_attempt_id IS NULL OR run_attempt_id = ''",
    "block_verification_reports": "audit_status IS NULL",
    "insight_quality_evaluations": "rubric_ref IS NULL",
}
IN_PLACE_METADATA_BACKFILLS = frozenset(
    {
        "conversation_messages",
        "investigation_threads",
        "block_verification_reports",
        "insight_quality_evaluations",
    }
)
IN_PLACE_ADDITIVE_TABLES = frozenset(
    {
        "agent_thread_summaries",
        "agent_generated_artifacts",
        "narrative_quality_audit_results",
        "controlled_investigation_operations",
        "controlled_investigation_dispatches",
    }
)
OBSOLETE_TABLES = (
    "analysis_runtime_publications",
    "analysis_assets",
    "answer_package_artifacts",
    "answer_packages",
    "claim_evidence_links",
    "claim_provenance_records",
    "clarification_execution_attempts",
    "clarification_resolutions",
    "clarification_resume_claims",
    "evidence_manifests",
    "evidence_refs",
    "investigation_artifacts",
    "query_repair_attempts",
    "result_refs",
    "verified_claims",
)
CURRENT_DISPATCH_KINDS = frozenset({"thread_message", "clarification_resolution"})
RETIRED_DISPATCH_KINDS = frozenset(
    {"artifact_continue", "clarification_resume", "clarification_retry"}
)
PRESERVED_CONTRACT_MIRROR_TABLES = frozenset(
    {"active_contracts", "contract_artifacts", "mirror_loads"}
)
PRESERVED_SINGLE_AUTHORITY_TABLES = frozenset({"schema_migrations"})


class SchemaCutoverError(RuntimeError):
    pass


def _schema_contract() -> tuple[str, tuple[str, ...]]:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    if SINGLE_AUTHORITY_MARKER not in schema:
        raise SchemaCutoverError("single_authority_schema_marker_missing")
    single_authority_slice = schema[schema.index(SINGLE_AUTHORITY_MARKER) :]
    migration_inserts = list(
        re.finditer(
            r"INSERT INTO waje_runtime\.schema_migrations\(migration_id, migration_digest\)",
            schema,
        )
    )
    if len(migration_inserts) != 1:
        raise SchemaCutoverError("single_authority_migration_boundary_invalid")
    ddl = schema[: migration_inserts[0].start()].rstrip() + "\n"
    digest = hashlib.sha256(ddl.encode()).hexdigest()
    if digest != SINGLE_AUTHORITY_MIGRATION_DIGEST:
        raise SchemaCutoverError("single_authority_schema_digest_conflict")
    declared_migrations = re.findall(
        r"'(?P<migration_id>single-authority-[a-z0-9.-]+)'",
        single_authority_slice,
    )
    if declared_migrations != [SINGLE_AUTHORITY_MIGRATION_ID]:
        raise SchemaCutoverError("single_authority_migration_identity_invalid")
    declared_tables = set(
        re.findall(
            r"CREATE TABLE IF NOT EXISTS waje_runtime\.([a-z0-9_]+)",
            single_authority_slice,
        )
    )
    if not PRESERVED_SINGLE_AUTHORITY_TABLES.issubset(declared_tables):
        raise SchemaCutoverError("single_authority_migration_ledger_missing")
    tables = tuple(
        sorted(
            declared_tables
            - PRESERVED_SINGLE_AUTHORITY_TABLES
            - PRESERVED_CONTRACT_MIRROR_TABLES
        )
    )
    if (
        not tables
        or set(tables).intersection(PRESERVED_CONTRACT_MIRROR_TABLES)
        or set(tables).intersection(OBSOLETE_TABLES)
    ):
        raise SchemaCutoverError("single_authority_schema_table_scope_invalid")
    migration_digest_literal = re.search(
        rf"'{re.escape(SINGLE_AUTHORITY_MIGRATION_ID)}',\s*'([0-9a-f]{{64}})'",
        single_authority_slice,
    )
    if (
        migration_digest_literal is None
        or migration_digest_literal.group(1) != SINGLE_AUTHORITY_MIGRATION_DIGEST
    ):
        raise SchemaCutoverError("single_authority_migration_digest_literal_invalid")
    return schema, tables


def _declared_schema_tables(schema: str) -> frozenset[str]:
    tables = frozenset(
        re.findall(
            r"CREATE TABLE IF NOT EXISTS waje_runtime\.([a-z0-9_]+)",
            schema,
        )
    )
    if not tables or tables.intersection(OBSOLETE_TABLES):
        raise SchemaCutoverError("declared_schema_table_scope_invalid")
    return tables


def _unexpected_live_tables(schema: str, live_tables: set[str]) -> tuple[str, ...]:
    allowed = (
        _declared_schema_tables(schema)
        | PRESERVED_CONTRACT_MIRROR_TABLES
        | frozenset(OBSOLETE_TABLES)
    )
    return tuple(sorted(live_tables - allowed))


def _table_names(connection: Any) -> set[str]:
    rows = connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'waje_runtime'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_counts(connection: Any, tables: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in sorted(tables):
        if re.fullmatch(r"[a-z0-9_]+", table) is None:
            raise SchemaCutoverError("cutover_table_identifier_invalid")
        counts[table] = int(
            connection.execute(f"SELECT count(*) FROM waje_runtime.{table}").fetchone()[
                0
            ]
        )
    return counts


def _single_authority_migrations(connection: Any) -> list[tuple[str, str]]:
    return [
        (str(migration_id), str(migration_digest))
        for migration_id, migration_digest in connection.execute(
            """
            SELECT migration_id, migration_digest
            FROM waje_runtime.schema_migrations
            WHERE migration_id LIKE 'single-authority-%'
            ORDER BY migration_id
            """
        ).fetchall()
    ]


def _lock_reset_tables(
    connection: Any, single_authority_tables: tuple[str, ...]
) -> None:
    live_tables = _table_names(connection)
    reset_tables = sorted(
        (
            set(single_authority_tables)
            | set(OBSOLETE_TABLES)
            | PRESERVED_SINGLE_AUTHORITY_TABLES
        )
        & live_tables
    )
    for table in reset_tables:
        if not re.fullmatch(r"[a-z0-9_]+", table):
            raise SchemaCutoverError("cutover_table_identifier_invalid")
    if reset_tables:
        qualified_tables = ", ".join(f"waje_runtime.{table}" for table in reset_tables)
        connection.execute(f"LOCK TABLE {qualified_tables} IN ACCESS EXCLUSIVE MODE")


def audit_cutover(connection: Any) -> dict[str, Any]:
    schema, single_authority_tables = _schema_contract()
    live_tables = _table_names(connection)
    unexpected_live_tables = _unexpected_live_tables(schema, live_tables)
    if unexpected_live_tables:
        raise SchemaCutoverError(
            "schema_cutover_unknown_tables:" + ",".join(unexpected_live_tables)
        )
    if "run_dispatches" not in live_tables:
        raise SchemaCutoverError("run_dispatches_table_required")
    live_table_counts: dict[str, int] = {}
    for table in sorted(live_tables):
        if re.fullmatch(r"[a-z0-9_]+", table) is None:
            raise SchemaCutoverError("cutover_table_identifier_invalid")
        live_table_counts[table] = int(
            connection.execute(f"SELECT count(*) FROM waje_runtime.{table}").fetchone()[
                0
            ]
        )
    obsolete_counts = {
        table: live_table_counts[table]
        for table in OBSOLETE_TABLES
        if table in live_table_counts
    }
    single_authority_counts = {
        table: live_table_counts[table]
        for table in single_authority_tables
        if table in live_table_counts
    }
    dispatch_counts = {
        str(kind): int(count)
        for kind, count in connection.execute(
            """
            SELECT producer_kind, count(*)
            FROM waje_runtime.run_dispatches
            GROUP BY producer_kind
            ORDER BY producer_kind
            """
        ).fetchall()
    }
    unknown_dispatch_kinds = set(dispatch_counts) - (
        CURRENT_DISPATCH_KINDS | RETIRED_DISPATCH_KINDS
    )
    if unknown_dispatch_kinds:
        raise SchemaCutoverError(
            "run_dispatch_kind_outside_cutover_contract:"
            + ",".join(sorted(unknown_dispatch_kinds))
        )
    nonterminal_dispatch_count = int(
        connection.execute(
            """
            SELECT count(*)
            FROM waje_runtime.run_dispatches
            WHERE dispatch_state <> 'terminal'
            """
        ).fetchone()[0]
    )
    null_message_dispatch_count = int(
        connection.execute(
            """
            SELECT count(*)
            FROM waje_runtime.run_dispatches
            WHERE message_id IS NULL
            """
        ).fetchone()[0]
    )
    pre_v8_run_counts = {
        str(status): int(count)
        for status, count in connection.execute(
            """
            SELECT status, count(*)
            FROM waje_runtime.analysis_runs
            WHERE status <> 'failed'
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
    }
    pending_clarification_thread_count = int(
        connection.execute(
            """
            SELECT count(*)
            FROM waje_runtime.investigation_threads
            WHERE pending_clarification_id <> ''
               OR pending_clarification_topic_id IS NOT NULL
            """
        ).fetchone()[0]
    )
    source_migrations = (
        [
            (str(migration_id), str(migration_digest))
            for migration_id, migration_digest in connection.execute(
                """
                SELECT migration_id, migration_digest
                FROM waje_runtime.schema_migrations
                WHERE migration_id LIKE 'single-authority-%'
                ORDER BY migration_id
                """
            ).fetchall()
        ]
        if "schema_migrations" in live_tables
        else []
    )
    if source_migrations != [(SOURCE_MIGRATION_ID, SOURCE_MIGRATION_DIGEST)]:
        raise SchemaCutoverError("schema_cutover_source_migration_invalid")
    nonempty_single_authority = {
        table: count for table, count in single_authority_counts.items() if count
    }
    nonempty_obsolete = {
        table: count for table, count in obsolete_counts.items() if count
    }
    retired_dispatch_count = sum(
        dispatch_counts.get(kind, 0) for kind in RETIRED_DISPATCH_KINDS
    )
    return {
        "single_authority_migration_id": SINGLE_AUTHORITY_MIGRATION_ID,
        "single_authority_migration_digest": SINGLE_AUTHORITY_MIGRATION_DIGEST,
        "single_authority_table_count": len(single_authority_tables),
        "single_authority_existing_table_count": len(single_authority_counts),
        "single_authority_nonempty_tables": nonempty_single_authority,
        "obsolete_table_counts": obsolete_counts,
        "obsolete_nonempty_tables": nonempty_obsolete,
        "dispatch_counts": dispatch_counts,
        "dispatch_count": sum(dispatch_counts.values()),
        "retired_dispatch_count": retired_dispatch_count,
        "nonterminal_dispatch_count": nonterminal_dispatch_count,
        "null_message_dispatch_count": null_message_dispatch_count,
        "pre_v8_run_counts": pre_v8_run_counts,
        "pre_v8_run_count": sum(pre_v8_run_counts.values()),
        "pending_clarification_thread_count": (pending_clarification_thread_count),
        "source_migrations": source_migrations,
        "live_table_counts": live_table_counts,
        "schema_migrations_present": "schema_migrations" in live_tables,
        "preserved_contract_mirror_tables": sorted(
            PRESERVED_CONTRACT_MIRROR_TABLES.intersection(live_tables)
        ),
    }


def _destructive_data_reasons(audit: dict[str, Any]) -> dict[str, Any]:
    reasons: dict[str, Any] = {}
    if audit["single_authority_nonempty_tables"]:
        reasons["single_authority_tables"] = audit["single_authority_nonempty_tables"]
    if audit["obsolete_nonempty_tables"]:
        reasons["obsolete_tables"] = audit["obsolete_nonempty_tables"]
    if audit["dispatch_count"]:
        reasons["dispatches_rebuilt"] = audit["dispatch_count"]
    if audit["pre_v8_run_count"]:
        reasons["pre_v8_runs"] = audit["pre_v8_run_count"]
    if audit["pending_clarification_thread_count"]:
        reasons["pending_clarification_threads"] = audit[
            "pending_clarification_thread_count"
        ]
    return reasons


def _validated_backup_ref(audit: dict[str, Any], backup_ref: str | None) -> str | None:
    normalized = backup_ref.strip() if backup_ref is not None else None
    if normalized == "":
        normalized = None
    if _destructive_data_reasons(audit) and normalized is None:
        raise SchemaCutoverError("cutover_nonempty_data_requires_backup_ref")
    if normalized is None:
        return None
    path = Path(normalized)
    if not path.is_absolute() or not path.is_file():
        raise SchemaCutoverError("cutover_backup_artifact_invalid")
    archive_digest = _sha256_path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise SchemaCutoverError("cutover_backup_artifact_invalid")
            names = archive.namelist()
            if (
                len(names) != len(set(names))
                or "manifest.json" not in names
                or "schema/conversation-runtime.sql" not in names
                or "schema/live-schema-metadata.json" not in names
            ):
                raise SchemaCutoverError("cutover_backup_manifest_invalid")
            manifest = json.loads(archive.read("manifest.json"))
            metadata = json.loads(archive.read("schema/live-schema-metadata.json"))
            table_records = manifest.get("tables")
            live_table_counts = audit.get("live_table_counts")
            if (
                manifest.get("backup_format") != "waje-runtime-csv-zip.v1"
                or manifest.get("schema") != "waje_runtime"
                or not isinstance(live_table_counts, dict)
                or not live_table_counts
                or not isinstance(manifest.get("schema_contract_sha256"), str)
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    manifest["schema_contract_sha256"],
                )
                or not isinstance(table_records, list)
                or not table_records
            ):
                raise SchemaCutoverError("cutover_backup_manifest_invalid")
            schema_digest = hashlib.sha256(
                archive.read("schema/conversation-runtime.sql")
            ).hexdigest()
            if schema_digest != manifest["schema_contract_sha256"]:
                raise SchemaCutoverError("cutover_backup_member_digest_invalid")
            validated_tables: set[str] = set()
            for record in table_records:
                if not isinstance(record, dict) or set(record) != {
                    "member",
                    "row_count",
                    "sha256",
                    "table",
                    "uncompressed_bytes",
                }:
                    raise SchemaCutoverError("cutover_backup_manifest_invalid")
                table = record["table"]
                member = record["member"]
                if (
                    not isinstance(table, str)
                    or not re.fullmatch(r"[a-z0-9_]+", table)
                    or table in validated_tables
                    or member != f"tables/{table}.csv"
                    or member not in names
                    or not isinstance(record["row_count"], int)
                    or record["row_count"] < 0
                    or not isinstance(record["uncompressed_bytes"], int)
                    or record["uncompressed_bytes"] < 0
                    or not isinstance(record["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                ):
                    raise SchemaCutoverError("cutover_backup_manifest_invalid")
                with archive.open(member) as member_file:
                    member_digest, member_size = _sha256_stream(member_file)
                if (
                    member_digest != record["sha256"]
                    or member_size != record["uncompressed_bytes"]
                ):
                    raise SchemaCutoverError("cutover_backup_member_digest_invalid")
                validated_tables.add(table)
            if not {"run_dispatches", "schema_migrations"}.issubset(
                validated_tables
            ) or validated_tables != set(live_table_counts):
                raise SchemaCutoverError("cutover_backup_manifest_invalid")
            manifest_counts = {
                str(record["table"]): int(record["row_count"])
                for record in table_records
            }
            if manifest_counts != {
                str(table): int(count) for table, count in live_table_counts.items()
            }:
                raise SchemaCutoverError("cutover_backup_snapshot_stale")
            expected_migration = {
                "migration_id": SOURCE_MIGRATION_ID,
                "migration_digest": SOURCE_MIGRATION_DIGEST,
            }
            if not isinstance(metadata, dict) or metadata.get("migration_ledger") != [
                expected_migration
            ]:
                raise SchemaCutoverError("cutover_backup_source_migration_invalid")
            with archive.open("tables/schema_migrations.csv") as member:
                migration_rows = list(
                    csv.DictReader(io.TextIOWrapper(member, encoding="utf-8"))
                )
            single_authority_rows = [
                {
                    "migration_id": str(row.get("migration_id") or ""),
                    "migration_digest": str(row.get("migration_digest") or ""),
                }
                for row in migration_rows
                if str(row.get("migration_id") or "").startswith("single-authority-")
            ]
            if single_authority_rows != [expected_migration]:
                raise SchemaCutoverError("cutover_backup_source_migration_invalid")
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        if isinstance(exc, SchemaCutoverError):
            raise
        raise SchemaCutoverError("cutover_backup_artifact_invalid") from exc
    return f"{path}#sha256={archive_digest}"


def _sha256_path(path: Path) -> str:
    with path.open("rb") as source:
        return _sha256_stream(source)[0]


def _sha256_stream(source: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _validate_current_runtime_schema(connection: Any) -> None:
    constraint_rows = connection.execute(
        """
        SELECT conname, convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'waje_runtime.run_dispatches'::regclass
        """
    ).fetchall()
    constraints = {
        str(name): (bool(validated), str(definition))
        for name, validated, definition in constraint_rows
    }
    required_constraints = {
        "run_dispatch_producer_kind_check",
        "run_dispatch_request_digest_check",
        "run_dispatch_request_payload_check",
        "run_dispatch_scope_shape_check",
        "run_dispatch_owner_shape_check",
        "run_dispatch_state_check",
    }
    if not required_constraints.issubset(constraints) or any(
        constraints[name][0] is not True for name in required_constraints
    ):
        raise SchemaCutoverError("run_dispatch_constraint_not_validated")
    producer_definition = constraints["run_dispatch_producer_kind_check"][1]
    if not all(kind in producer_definition for kind in CURRENT_DISPATCH_KINDS):
        raise SchemaCutoverError("run_dispatch_producer_contract_invalid")
    definitions = [definition for _, definition in constraints.values()]
    normalized_definitions = [
        re.sub(r"\s+", " ", definition).strip().lower() for definition in definitions
    ]
    if not any(
        "unique (producer_kind, scope_ref, request_identity)" in definition
        for definition in normalized_definitions
    ) or any(
        re.fullmatch(r"unique \(run_id\)", definition)
        for definition in normalized_definitions
    ):
        raise SchemaCutoverError("run_dispatch_unique_contract_invalid")
    nullable = connection.execute(
        """
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'waje_runtime'
          AND table_name = 'run_dispatches'
          AND column_name = 'message_id'
        """
    ).fetchone()
    if nullable != ("NO",):
        raise SchemaCutoverError("run_dispatch_message_contract_invalid")
    quality_columns = {
        str(column_name): (str(is_nullable), str(data_type))
        for column_name, is_nullable, data_type in connection.execute(
            """
            SELECT column_name, is_nullable, data_type
            FROM information_schema.columns
            WHERE table_schema = 'waje_runtime'
              AND table_name = 'insight_quality_evaluations'
              AND column_name IN (
                'rubric_ref',
                'rubric_digest',
                'rubric',
                'evaluation_case_ref',
                'evaluation_case_digest',
                'evaluation_case',
                'model_profile_ref',
                'model_profile_digest',
                'model_profile',
                'human_reasons'
              )
            """
        ).fetchall()
    }
    expected_quality_columns = {
        "rubric_ref": ("NO", "text"),
        "rubric_digest": ("NO", "text"),
        "rubric": ("NO", "jsonb"),
        "evaluation_case_ref": ("NO", "text"),
        "evaluation_case_digest": ("NO", "text"),
        "evaluation_case": ("NO", "jsonb"),
        "model_profile_ref": ("NO", "text"),
        "model_profile_digest": ("NO", "text"),
        "model_profile": ("NO", "jsonb"),
        "human_reasons": ("NO", "jsonb"),
    }
    if quality_columns != expected_quality_columns:
        raise SchemaCutoverError("insight_quality_evaluation_schema_invalid")
    index_rows = connection.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'waje_runtime'
          AND tablename = 'run_dispatches'
        """
    ).fetchall()
    indexes = {str(name): str(definition) for name, definition in index_rows}
    active_index = indexes.get("idx_run_dispatch_one_active_per_run", "")
    if (
        "CREATE UNIQUE INDEX" not in active_index
        or "(run_id)" not in active_index
        or "WHERE" not in active_index
        or not all(state in active_index for state in ("pending", "leased", "running"))
        or not any(
            "CREATE UNIQUE INDEX" in definition and "(message_id)" in definition
            for definition in indexes.values()
        )
    ):
        raise SchemaCutoverError("run_dispatch_index_contract_invalid")
    trigger = connection.execute(
        """
        SELECT tgenabled
        FROM pg_trigger
        WHERE tgrelid = 'waje_runtime.run_dispatches'::regclass
          AND tgname = 'run_dispatch_command_immutable'
          AND NOT tgisinternal
        """
    ).fetchone()
    if trigger is None or trigger[0] not in {"O", "A"}:
        raise SchemaCutoverError("run_dispatch_immutable_trigger_invalid")
    invalid_counts = connection.execute(
        """
        SELECT
          count(*) FILTER (
            WHERE producer_kind NOT IN (
              'thread_message', 'clarification_resolution'
            )
          ),
          count(*) FILTER (WHERE message_id IS NULL),
          count(*) FILTER (
            WHERE NOT (
              (producer_kind = 'thread_message' AND scope_ref = thread_id)
              OR (
                producer_kind = 'clarification_resolution'
                AND scope_ref = run_id
              )
            )
          ),
          count(*) FILTER (WHERE length(request_digest) <> 64),
          count(*) FILTER (
            WHERE jsonb_typeof(request_payload) <> 'object'
          ),
          (
            SELECT count(*)
            FROM (
              SELECT run_id
              FROM waje_runtime.run_dispatches
              WHERE dispatch_state IN ('pending', 'leased', 'running')
              GROUP BY run_id
              HAVING count(*) > 1
            ) duplicate_active
          )
        FROM waje_runtime.run_dispatches
        """
    ).fetchone()
    if invalid_counts is None or tuple(int(value) for value in invalid_counts) != (
        0,
        0,
        0,
        0,
        0,
        0,
    ):
        raise SchemaCutoverError("run_dispatch_data_contract_invalid")


def _validate_v8_runtime_state(connection: Any) -> None:
    _validate_current_runtime_schema(connection)
    pre_v8_runs = connection.execute(
        """
        SELECT count(*)
        FROM waje_runtime.analysis_runs
        WHERE status <> 'failed'
        """
    ).fetchone()
    pending = connection.execute(
        """
        SELECT count(*)
        FROM waje_runtime.investigation_threads
        WHERE pending_clarification_id <> ''
           OR pending_clarification_topic_id IS NOT NULL
        """
    ).fetchone()
    if pre_v8_runs != (0,) or pending != (0,):
        raise SchemaCutoverError("schema_cutover_pre_v8_state_remaining")


def _validate_in_place_schema_sql(schema: str) -> None:
    forbidden_patterns = {
        "in_place_drop_schema_forbidden": r"\bDROP\s+SCHEMA\b",
        "in_place_drop_table_forbidden": r"\bDROP\s+TABLE\b",
        "in_place_truncate_forbidden": r"\bTRUNCATE\b",
        "in_place_delete_rows_forbidden": r"\bDELETE\s+FROM\b",
        "in_place_drop_column_forbidden": (
            r"\bALTER\s+TABLE\s+waje_runtime\.[a-z0-9_]+"
            r"[\s\S]*?\bDROP\s+COLUMN\b"
        ),
    }
    for error, pattern in forbidden_patterns.items():
        if re.search(pattern, schema, flags=re.IGNORECASE):
            raise SchemaCutoverError(error)
    update_targets = tuple(
        match.group(1)
        for match in re.finditer(
            r"^UPDATE\s+waje_runtime\.([a-z0-9_]+)\b",
            schema,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    )
    if update_targets != tuple(IN_PLACE_BACKFILL_PREDICATES):
        raise SchemaCutoverError("in_place_update_scope_invalid")


def _validate_in_place_backfills_are_noop(connection: Any) -> None:
    for table, predicate in IN_PLACE_BACKFILL_PREDICATES.items():
        if (
            re.fullmatch(r"[a-z0-9_]+", table) is None
            or ";" in predicate
            or "--" in predicate
        ):
            raise SchemaCutoverError("in_place_backfill_contract_invalid")
        if table in IN_PLACE_METADATA_BACKFILLS:
            continue
        count = int(
            connection.execute(
                f"SELECT count(*) FROM waje_runtime.{table} WHERE {predicate}"
            ).fetchone()[0]
        )
        if count:
            raise SchemaCutoverError(f"in_place_backfill_would_mutate:{table}:{count}")


def apply_in_place_upgrade(connection: Any) -> dict[str, Any]:
    schema, _ = _schema_contract()
    _validate_in_place_schema_sql(schema)
    expected_tables = set(_declared_schema_tables(schema)) | set(
        PRESERVED_CONTRACT_MIRROR_TABLES
    )
    try:
        connection.execute(
            "LOCK TABLE waje_runtime.schema_migrations IN EXCLUSIVE MODE"
        )
        source_migrations = _single_authority_migrations(connection)
        if len(source_migrations) != 1:
            raise SchemaCutoverError("in_place_upgrade_source_migration_invalid")
        source_contract = tuple(source_migrations[0])
        expected_additive_tables = IN_PLACE_SOURCE_CONTRACTS.get(source_contract)
        if expected_additive_tables is None:
            raise SchemaCutoverError("in_place_upgrade_source_migration_invalid")

        live_tables = _table_names(connection)
        missing = expected_tables - live_tables
        unexpected = live_tables - expected_tables
        if missing != set(expected_additive_tables) or unexpected:
            detail = json.dumps(
                {
                    "missing": sorted(missing),
                    "unexpected": sorted(unexpected),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            raise SchemaCutoverError(
                f"in_place_upgrade_table_contract_invalid:{detail}"
            )
        _validate_in_place_backfills_are_noop(connection)
        before_counts = _table_counts(connection, live_tables)

        connection.execute(schema)

        upgraded_tables = _table_names(connection)
        if upgraded_tables != expected_tables:
            raise SchemaCutoverError("in_place_upgrade_table_contract_changed")
        after_counts = _table_counts(connection, upgraded_tables)
        business_counts_before = {
            table: count
            for table, count in before_counts.items()
            if table != "schema_migrations"
        }
        business_counts_after = {
            table: count
            for table, count in after_counts.items()
            if table != "schema_migrations"
        }
        preserved_counts_after = {
            table: business_counts_after[table]
            for table in business_counts_before
        }
        additive_counts = {
            table: business_counts_after[table]
            for table in expected_additive_tables
        }
        if preserved_counts_after != business_counts_before:
            raise SchemaCutoverError("in_place_upgrade_business_rows_changed")
        if any(additive_counts.values()):
            raise SchemaCutoverError("in_place_upgrade_additive_tables_not_empty")
        _validate_current_runtime_schema(connection)

        deleted = connection.execute(
            """
            DELETE FROM waje_runtime.schema_migrations
            WHERE migration_id = %s AND migration_digest = %s
            RETURNING migration_id
            """,
            (
                source_contract[0],
                source_contract[1],
            ),
        ).fetchall()
        if deleted != [(source_contract[0],)]:
            raise SchemaCutoverError("in_place_upgrade_source_delete_invalid")
        migrations = _single_authority_migrations(connection)
        if migrations != [
            (
                SINGLE_AUTHORITY_MIGRATION_ID,
                SINGLE_AUTHORITY_MIGRATION_DIGEST,
            )
        ]:
            raise SchemaCutoverError("in_place_upgrade_target_migration_invalid")
        connection.commit()
        return {
            "applied": True,
            "business_row_counts": business_counts_after,
            "source_migration_id": source_contract[0],
            "target_migration_digest": SINGLE_AUTHORITY_MIGRATION_DIGEST,
            "target_migration_id": SINGLE_AUTHORITY_MIGRATION_ID,
        }
    except Exception:
        connection.rollback()
        raise


def apply_cutover(
    connection: Any,
    *,
    development_reset: bool = False,
    backup_ref: str | None = None,
    retire_pre_v8_runs: bool = False,
) -> dict[str, Any]:
    if not development_reset:
        raise SchemaCutoverError("cutover_requires_explicit_development_reset")
    schema, single_authority_tables = _schema_contract()
    try:
        connection.execute(
            "LOCK TABLE waje_runtime.run_dispatches, "
            "waje_runtime.analysis_runs IN ACCESS EXCLUSIVE MODE"
        )
        _lock_reset_tables(connection, single_authority_tables)
        audit = audit_cutover(connection)
        validated_backup_ref = _validated_backup_ref(audit, backup_ref)
        retirement_required = bool(
            audit["pre_v8_run_count"]
            or audit["pending_clarification_thread_count"]
            or audit["nonterminal_dispatch_count"]
        )
        if retirement_required and not retire_pre_v8_runs:
            raise SchemaCutoverError("cutover_pre_v8_runs_require_explicit_retirement")
        reset_payload = {
            "backup_ref": validated_backup_ref,
            "destructive_data": _destructive_data_reasons(audit),
            "migration_digest": SINGLE_AUTHORITY_MIGRATION_DIGEST,
            "migration_id": SINGLE_AUTHORITY_MIGRATION_ID,
        }
        connection.execute(
            """
            INSERT INTO waje_runtime.audit_events(
              event_type, actor_id, ref, payload
            )
            VALUES (
              'single_authority_development_schema_reset',
              'schema-cutover', %(migration_id)s, %(payload)s::jsonb
            )
            """,
            {
                "migration_id": SINGLE_AUTHORITY_MIGRATION_ID,
                "payload": json.dumps(
                    reset_payload, ensure_ascii=False, sort_keys=True
                ),
            },
        )
        retired_pre_v8_runs: list[tuple[Any, ...]] = []
        if audit["pre_v8_run_count"]:
            connection.execute(
                """
                INSERT INTO waje_runtime.audit_events(
                  event_type, thread_id, run_id, ref, payload
                )
                SELECT 'development_cutover_pre_v8_run_retired',
                       run.thread_id, run.run_id, run.run_id,
                       jsonb_build_object(
                         'run_status', run.status,
                         'reason', 'single_authority_schema_cutover'
                       )
                FROM waje_runtime.analysis_runs run
                WHERE run.status <> 'failed'
                """
            )
            retired_pre_v8_runs = connection.execute(
                """
                UPDATE waje_runtime.analysis_runs run
                SET status = 'failed',
                    request = COALESCE(run.request, '{}'::jsonb)
                      || jsonb_build_object(
                        'failure_reason',
                        'development_cutover_retired_pre_v8_run',
                        'failure_type', 'schema_cutover'
                      ),
                    updated_at = now()
                WHERE run.status <> 'failed'
                RETURNING run.run_id
                """
            ).fetchall()
        if audit["pending_clarification_thread_count"]:
            connection.execute(
                """
                INSERT INTO waje_runtime.audit_events(
                  event_type, thread_id, run_id, ref, payload
                )
                SELECT 'development_cutover_pending_clarification_cleared',
                       thread_id, NULLIF(pending_clarification_id, ''),
                       thread_id,
                       jsonb_build_object(
                         'pending_clarification_id',
                         pending_clarification_id,
                         'pending_clarification_topic_id',
                         pending_clarification_topic_id,
                         'reason', 'single_authority_schema_cutover'
                       )
                FROM waje_runtime.investigation_threads
                WHERE pending_clarification_id <> ''
                   OR pending_clarification_topic_id IS NOT NULL
                """
            )
            connection.execute(
                """
                UPDATE waje_runtime.investigation_threads
                SET pending_clarification_id = '',
                    pending_clarification_topic_id = NULL,
                    updated_at = now()
                WHERE pending_clarification_id <> ''
                   OR pending_clarification_topic_id IS NOT NULL
                """
            )
        connection.execute(
            """
            INSERT INTO waje_runtime.audit_events(
              event_type, thread_id, run_id, ref, payload
            )
            SELECT 'single_authority_cutover_dispatch_rebuilt',
                   thread_id, run_id, dispatch_id,
                   jsonb_build_object(
                     'producer_kind', producer_kind,
                     'dispatch_state', dispatch_state,
                     'terminal_status', terminal_status,
                     'failure_reason', failure_reason
                   )
            FROM waje_runtime.run_dispatches
            """,
        )
        connection.execute("DROP TABLE waje_runtime.run_dispatches")
        if audit["schema_migrations_present"]:
            connection.execute(
                """
                DELETE FROM waje_runtime.schema_migrations
                WHERE migration_id LIKE 'single-authority-%'
                """
            )
        for table in sorted(set(OBSOLETE_TABLES).union(single_authority_tables)):
            if not re.fullmatch(r"[a-z0-9_]+", table):
                raise SchemaCutoverError("cutover_table_identifier_invalid")
            connection.execute(f"DROP TABLE IF EXISTS waje_runtime.{table} CASCADE")
        connection.execute(schema)

        live_tables = _table_names(connection)
        missing_declared_tables = sorted(_declared_schema_tables(schema) - live_tables)
        remaining_obsolete = sorted(set(OBSOLETE_TABLES).intersection(live_tables))
        unexpected_live_tables = _unexpected_live_tables(schema, live_tables)
        missing_preserved_contract_mirrors = sorted(
            set(audit["preserved_contract_mirror_tables"]) - live_tables
        )
        if (
            missing_declared_tables
            or remaining_obsolete
            or unexpected_live_tables
            or missing_preserved_contract_mirrors
        ):
            raise SchemaCutoverError("schema_cutover_table_validation_failed")
        migrations = connection.execute(
            """
            SELECT migration_id, migration_digest
            FROM waje_runtime.schema_migrations
            WHERE migration_id LIKE 'single-authority-%'
            ORDER BY migration_id
            """,
        ).fetchall()
        if migrations != [
            (
                SINGLE_AUTHORITY_MIGRATION_ID,
                SINGLE_AUTHORITY_MIGRATION_DIGEST,
            )
        ]:
            raise SchemaCutoverError("schema_cutover_migration_digest_invalid")
        _validate_v8_runtime_state(connection)
        connection.commit()
        return {
            **audit,
            "applied": True,
            "rebuilt_dispatch_count": audit["dispatch_count"],
            "retired_pre_v8_run_count": len(retired_pre_v8_runs),
            "dropped_obsolete_tables": sorted(audit["obsolete_table_counts"]),
            "rebuilt_single_authority_tables": len(single_authority_tables),
            "backup_ref": validated_backup_ref,
        }
    except Exception:
        connection.rollback()
        raise


def _connect() -> Any:
    database_url = os.environ.get("WAJE_RUNTIME_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not database_url:
        raise SchemaCutoverError("runtime_database_url_required")
    try:
        import psycopg
    except ImportError as exc:
        raise SchemaCutoverError("psycopg_required") from exc
    return psycopg.connect(
        database_url,
        options="-c waje.actor_id=system",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit or apply the development-only single-authority schema cutover."
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument(
        "--in-place-upgrade",
        action="store_true",
        help=(
            f"Promote {IN_PLACE_SOURCE_MIGRATION_ID} to "
            f"{SINGLE_AUTHORITY_MIGRATION_ID} without rebuilding business data."
        ),
    )
    parser.add_argument(
        "--development-reset",
        action="store_true",
        help=(
            "Explicitly authorize rebuilding the complete single-authority "
            "workflow slice."
        ),
    )
    parser.add_argument(
        "--backup-ref",
        help=(
            "Reference to a completed external backup. Required when the "
            "cutover will remove or retire any persisted data."
        ),
    )
    parser.add_argument(
        "--retire-pre-v8-runs",
        action="store_true",
        help=(
            "Explicitly retire every run created under the source schema, clear "
            "stale clarification pointers, and rebuild dispatch commands."
        ),
    )
    args = parser.parse_args()
    if args.apply and not args.development_reset:
        parser.error("--apply requires --development-reset")
    if args.development_reset and not args.apply:
        parser.error("--development-reset requires --apply")
    if args.backup_ref is not None and not args.apply:
        parser.error("--backup-ref requires --apply")
    if args.retire_pre_v8_runs and not args.apply:
        parser.error("--retire-pre-v8-runs requires --apply")
    connection = _connect()
    try:
        if args.in_place_upgrade:
            result = apply_in_place_upgrade(connection)
        elif args.apply:
            result = apply_cutover(
                connection,
                development_reset=args.development_reset,
                backup_ref=args.backup_ref,
                retire_pre_v8_runs=args.retire_pre_v8_runs,
            )
        else:
            result = audit_cutover(connection)
            connection.rollback()
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
