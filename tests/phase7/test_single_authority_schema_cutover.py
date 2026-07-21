from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from tools.runtime import cutover_single_authority_schema as cutover_module
from tools.runtime.cutover_single_authority_schema import (
    CURRENT_DISPATCH_KINDS,
    IN_PLACE_METADATA_BACKFILLS,
    IN_PLACE_SOURCE_MIGRATION_DIGEST,
    IN_PLACE_SOURCE_MIGRATION_ID,
    OBSOLETE_TABLES,
    PRESERVED_CONTRACT_MIRROR_TABLES,
    PRESERVED_SINGLE_AUTHORITY_TABLES,
    RETIRED_DISPATCH_KINDS,
    SOURCE_MIGRATION_DIGEST,
    SOURCE_MIGRATION_ID,
    SINGLE_AUTHORITY_MIGRATION_DIGEST,
    SINGLE_AUTHORITY_MIGRATION_ID,
    SchemaCutoverError,
    _destructive_data_reasons,
    _schema_contract,
    _validate_in_place_schema_sql,
    _validate_v8_runtime_state,
    _validated_backup_ref,
    audit_cutover,
    apply_cutover,
    apply_in_place_upgrade,
)


def _audit(**overrides: object) -> dict[str, object]:
    audit: dict[str, object] = {
        "single_authority_nonempty_tables": {},
        "obsolete_nonempty_tables": {},
        "dispatch_count": 0,
        "pre_v8_run_count": 0,
        "pending_clarification_thread_count": 0,
        "live_table_counts": {
            "run_dispatches": 0,
            "schema_migrations": 1,
        },
    }
    audit.update(overrides)
    return audit


def test_cutover_is_pinned_to_the_complete_single_authority_slice() -> None:
    schema, tables = _schema_contract()

    assert SINGLE_AUTHORITY_MIGRATION_ID == "single-authority-workflow.v10"
    assert SOURCE_MIGRATION_ID == "single-authority-workflow.v7"
    assert SOURCE_MIGRATION_DIGEST == (
        "b735fa8fb3d888a3d12be7f335711956e37ba4fc344d294bfbee59a92ac5e3cf"
    )
    assert IN_PLACE_SOURCE_MIGRATION_ID == "single-authority-workflow.v9"
    assert IN_PLACE_SOURCE_MIGRATION_DIGEST == (
        "76216d3271244e452531bf563b5c3fa1344dcb499c04a78000452259d00817b1"
    )
    assert IN_PLACE_METADATA_BACKFILLS == {
        "conversation_messages",
        "investigation_threads",
    }
    assert len(SINGLE_AUTHORITY_MIGRATION_DIGEST) == 64
    assert len(tables) == 73
    for table in (
        "intent_revisions",
        "plan_revisions",
        "durable_call_attempts",
        "capability_task_attempts",
        "claim_graphs",
        "narrative_material_projections",
        "narrative_documents",
        "publication_revisions",
        "delivery_dispatches",
        "guardrail_promotion_records",
        "post_seal_failure_terminals",
    ):
        assert table in tables
    assert SINGLE_AUTHORITY_MIGRATION_ID in schema
    assert "single-authority-workflow.v7" not in schema
    assert schema.count("INSERT INTO waje_runtime.schema_migrations") == 1
    assert "single-authority-phase46.v4" not in schema


def test_cutover_scope_preserves_current_ledgers_and_targets_superseded_tables() -> (
    None
):
    schema, single_authority_tables = _schema_contract()

    assert set(OBSOLETE_TABLES) == {
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
    }
    assert PRESERVED_CONTRACT_MIRROR_TABLES == {
        "active_contracts",
        "contract_artifacts",
        "mirror_loads",
    }
    assert PRESERVED_SINGLE_AUTHORITY_TABLES == {"schema_migrations"}
    assert not set(single_authority_tables).intersection(
        PRESERVED_CONTRACT_MIRROR_TABLES
    )
    assert not set(single_authority_tables).intersection(OBSOLETE_TABLES)
    assert not set(OBSOLETE_TABLES).intersection(PRESERVED_CONTRACT_MIRROR_TABLES)
    for table in OBSOLETE_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS waje_runtime.{table}" not in schema
    assert CURRENT_DISPATCH_KINDS == {
        "thread_message",
        "clarification_resolution",
    }
    assert RETIRED_DISPATCH_KINDS == {
        "artifact_continue",
        "clarification_resume",
        "clarification_retry",
    }


def _backup_artifact(
    tmp_path: Path,
    *,
    tampered_digest: bool = False,
    dispatch_count: int = 0,
    source_migration_id: str = SOURCE_MIGRATION_ID,
    source_migration_digest: str = SOURCE_MIGRATION_DIGEST,
) -> tuple[Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "waje-runtime-v7-before-v8.zip"
    schema = b"CREATE SCHEMA waje_runtime;\n"
    table_members = {
        "run_dispatches": b"dispatch_id\n"
        + b"".join(f"dispatch-{index}\n".encode() for index in range(dispatch_count)),
        "schema_migrations": (
            b"migration_id,migration_digest\n"
            + source_migration_id.encode()
            + b","
            + source_migration_digest.encode()
            + b"\n"
        ),
    }
    records = [
        {
            "member": f"tables/{table}.csv",
            "row_count": dispatch_count if table == "run_dispatches" else 1,
            "sha256": hashlib.sha256(content).hexdigest(),
            "table": table,
            "uncompressed_bytes": len(content),
        }
        for table, content in table_members.items()
    ]
    if tampered_digest:
        records[0]["sha256"] = "0" * 64
    manifest = {
        "backup_format": "waje-runtime-csv-zip.v1",
        "created_at": "2026-07-19T00:00:00Z",
        "schema": "waje_runtime",
        "schema_contract_sha256": hashlib.sha256(schema).hexdigest(),
        "tables": records,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for table, content in table_members.items():
            archive.writestr(f"tables/{table}.csv", content)
        archive.writestr("schema/conversation-runtime.sql", schema)
        archive.writestr(
            "schema/live-schema-metadata.json",
            json.dumps(
                {
                    "migration_ledger": [
                        {
                            "migration_digest": source_migration_digest,
                            "migration_id": source_migration_id,
                        }
                    ]
                },
                sort_keys=True,
            ).encode(),
        )
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, sort_keys=True).encode(),
        )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_nonempty_data_requires_a_verified_backup_artifact(tmp_path: Path) -> None:
    audit = _audit(
        single_authority_nonempty_tables={"intent_revisions": 2},
        obsolete_nonempty_tables={"answer_packages": 1},
        dispatch_count=3,
        pre_v8_run_count=2,
        pending_clarification_thread_count=1,
        live_table_counts={
            "run_dispatches": 3,
            "schema_migrations": 1,
        },
    )

    assert _destructive_data_reasons(audit) == {
        "single_authority_tables": {"intent_revisions": 2},
        "obsolete_tables": {"answer_packages": 1},
        "dispatches_rebuilt": 3,
        "pre_v8_runs": 2,
        "pending_clarification_threads": 1,
    }
    with pytest.raises(
        SchemaCutoverError,
        match="cutover_nonempty_data_requires_backup_ref",
    ):
        _validated_backup_ref(audit, "  ")
    backup_path, digest = _backup_artifact(tmp_path, dispatch_count=3)
    assert _validated_backup_ref(audit, f" {backup_path} ") == (
        f"{backup_path}#sha256={digest}"
    )


def test_backup_validation_rejects_tampered_member(tmp_path: Path) -> None:
    backup_path, _ = _backup_artifact(tmp_path, tampered_digest=True)
    with pytest.raises(
        SchemaCutoverError,
        match="cutover_backup_member_digest_invalid",
    ):
        _validated_backup_ref(
            _audit(dispatch_count=1),
            str(backup_path),
        )


def test_backup_validation_rejects_a_stale_snapshot_or_wrong_source_ledger(
    tmp_path: Path,
) -> None:
    stale_path, _ = _backup_artifact(tmp_path, dispatch_count=1)
    with pytest.raises(
        SchemaCutoverError,
        match="cutover_backup_snapshot_stale",
    ):
        _validated_backup_ref(
            _audit(
                dispatch_count=2,
                live_table_counts={
                    "run_dispatches": 2,
                    "schema_migrations": 1,
                },
            ),
            str(stale_path),
        )

    wrong_path = tmp_path / "wrong-source.zip"
    generated_path, _ = _backup_artifact(
        tmp_path / "wrong",
        source_migration_id="single-authority-workflow.v6",
        source_migration_digest="f" * 64,
    )
    generated_path.rename(wrong_path)
    with pytest.raises(
        SchemaCutoverError,
        match="cutover_backup_source_migration_invalid",
    ):
        _validated_backup_ref(_audit(), str(wrong_path))


def test_empty_cutover_does_not_invent_a_backup_reference() -> None:
    assert _destructive_data_reasons(_audit()) == {}
    assert _validated_backup_ref(_audit(), None) is None


def test_audit_rejects_tables_outside_the_declared_schema_closed_set() -> None:
    class Result:
        def fetchall(self) -> list[tuple[str]]:
            return [("run_dispatches",), ("answer_package_shadow",)]

    class Connection:
        def execute(self, statement: str, _params: object = None) -> Result:
            assert "information_schema.tables" in statement
            return Result()

    with pytest.raises(
        SchemaCutoverError,
        match="schema_cutover_unknown_tables:answer_package_shadow",
    ):
        audit_cutover(Connection())


def test_apply_requires_explicit_development_reset_before_database_access() -> None:
    class NoDatabaseAccess:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("database must not be accessed")

    with pytest.raises(
        SchemaCutoverError,
        match="cutover_requires_explicit_development_reset",
    ):
        apply_cutover(NoDatabaseAccess())


def test_in_place_schema_contract_forbids_business_data_rebuilds() -> None:
    schema, _ = _schema_contract()

    _validate_in_place_schema_sql(schema)
    for statement, error in (
        ("DROP TABLE waje_runtime.analysis_runs", "in_place_drop_table_forbidden"),
        ("TRUNCATE waje_runtime.analysis_runs", "in_place_truncate_forbidden"),
        ("DELETE FROM waje_runtime.analysis_runs", "in_place_delete_rows_forbidden"),
        (
            "UPDATE waje_runtime.customer_publications SET status = 'failed'",
            "in_place_update_scope_invalid",
        ),
    ):
        with pytest.raises(SchemaCutoverError, match=error):
            _validate_in_place_schema_sql(statement)


def test_in_place_upgrade_replaces_only_the_verified_migration_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

    class Connection:
        def __init__(self) -> None:
            self.source_present = True
            self.target_present = False
            self.committed = False
            self.rolled_back = False
            self.statements: list[str] = []

        def execute(self, statement: str, params: object = None) -> Result:
            self.statements.append(statement)
            if (
                statement
                == "LOCK TABLE waje_runtime.schema_migrations IN EXCLUSIVE MODE"
            ):
                return Result([])
            if statement == "current-schema-ddl":
                self.target_present = True
                return Result([])
            if "DELETE FROM waje_runtime.schema_migrations" in statement:
                assert params == (
                    IN_PLACE_SOURCE_MIGRATION_ID,
                    IN_PLACE_SOURCE_MIGRATION_DIGEST,
                )
                assert self.source_present is True
                self.source_present = False
                return Result([(IN_PLACE_SOURCE_MIGRATION_ID,)])
            raise AssertionError(statement)

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    connection = Connection()
    monkeypatch.setattr(
        cutover_module,
        "_schema_contract",
        lambda: ("current-schema-ddl", ("run_dispatches",)),
    )
    monkeypatch.setattr(
        cutover_module,
        "_validate_in_place_schema_sql",
        lambda _schema: None,
    )
    monkeypatch.setattr(
        cutover_module,
        "_validate_in_place_backfills_are_noop",
        lambda _connection: None,
    )
    monkeypatch.setattr(
        cutover_module,
        "_declared_schema_tables",
        lambda _schema: frozenset({"run_dispatches", "schema_migrations"}),
    )
    monkeypatch.setattr(
        cutover_module,
        "PRESERVED_CONTRACT_MIRROR_TABLES",
        frozenset(),
    )
    monkeypatch.setattr(
        cutover_module,
        "_table_names",
        lambda _connection: {"run_dispatches", "schema_migrations"},
    )
    monkeypatch.setattr(
        cutover_module,
        "_table_counts",
        lambda _connection, _tables: {
            "run_dispatches": 19,
            "schema_migrations": 2 if connection.target_present else 1,
        },
    )
    monkeypatch.setattr(
        cutover_module,
        "_validate_current_runtime_schema",
        lambda _connection: None,
    )

    def migrations(_connection: object) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        if connection.source_present:
            rows.append(
                (
                    IN_PLACE_SOURCE_MIGRATION_ID,
                    IN_PLACE_SOURCE_MIGRATION_DIGEST,
                )
            )
        if connection.target_present:
            rows.append(
                (
                    SINGLE_AUTHORITY_MIGRATION_ID,
                    SINGLE_AUTHORITY_MIGRATION_DIGEST,
                )
            )
        return rows

    monkeypatch.setattr(
        cutover_module,
        "_single_authority_migrations",
        migrations,
    )

    result = apply_in_place_upgrade(connection)

    assert result["source_migration_id"] == IN_PLACE_SOURCE_MIGRATION_ID
    assert result["target_migration_id"] == SINGLE_AUTHORITY_MIGRATION_ID
    assert result["business_row_counts"] == {"run_dispatches": 19}
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.source_present is False
    assert connection.target_present is True


def test_in_place_upgrade_rejects_an_unverified_source_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.rolled_back = False

        def execute(self, statement: str, _params: object = None) -> object:
            assert statement == (
                "LOCK TABLE waje_runtime.schema_migrations IN EXCLUSIVE MODE"
            )
            return object()

        def rollback(self) -> None:
            self.rolled_back = True

    connection = Connection()
    monkeypatch.setattr(
        cutover_module,
        "_schema_contract",
        lambda: ("current-schema-ddl", ()),
    )
    monkeypatch.setattr(
        cutover_module,
        "_validate_in_place_schema_sql",
        lambda _schema: None,
    )
    monkeypatch.setattr(
        cutover_module,
        "_declared_schema_tables",
        lambda _schema: frozenset(),
    )
    monkeypatch.setattr(
        cutover_module,
        "_single_authority_migrations",
        lambda _connection: [
            (SINGLE_AUTHORITY_MIGRATION_ID, SINGLE_AUTHORITY_MIGRATION_DIGEST)
        ],
    )

    with pytest.raises(
        SchemaCutoverError,
        match="in_place_upgrade_source_migration_invalid",
    ):
        apply_in_place_upgrade(connection)

    assert connection.rolled_back is True


def test_apply_rolls_back_before_writes_when_persisted_data_has_no_backup() -> None:
    class Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

        def fetchone(self) -> tuple[object, ...] | None:
            return self.rows[0] if self.rows else None

    class AuditOnlyConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.rolled_back = False

        def execute(self, statement: str, _params: object = None) -> Result:
            self.statements.append(statement)
            if "information_schema.tables" in statement:
                return Result(
                    [
                        ("run_dispatches",),
                        ("analysis_runs",),
                        ("investigation_threads",),
                        ("schema_migrations",),
                        ("answer_packages",),
                        ("intent_revisions",),
                    ]
                )
            if "count(*) FROM waje_runtime.answer_packages" in statement:
                return Result([(1,)])
            if "count(*) FROM waje_runtime.intent_revisions" in statement:
                return Result([(2,)])
            if statement.startswith("SELECT count(*) FROM waje_runtime."):
                return Result([(0,)])
            if "GROUP BY producer_kind" in statement:
                return Result([("thread_message", 1)])
            if "WHERE dispatch_state <> 'terminal'" in statement:
                return Result([(0,)])
            if "WHERE message_id IS NULL" in statement:
                return Result([(0,)])
            if "GROUP BY status" in statement:
                return Result([])
            if "pending_clarification_id <> ''" in statement:
                return Result([(0,)])
            if "SELECT migration_id, migration_digest" in statement:
                return Result([(SOURCE_MIGRATION_ID, SOURCE_MIGRATION_DIGEST)])
            if statement.startswith("LOCK TABLE"):
                return Result([])
            raise AssertionError(f"unexpected database write: {statement}")

        def rollback(self) -> None:
            self.rolled_back = True

    connection = AuditOnlyConnection()
    with pytest.raises(
        SchemaCutoverError,
        match="cutover_nonempty_data_requires_backup_ref",
    ):
        apply_cutover(connection, development_reset=True)

    assert connection.rolled_back is True
    assert not any(
        "INSERT INTO waje_runtime.audit_events" in statement
        for statement in connection.statements
    )


def test_v8_post_apply_validation_checks_dispatch_and_runtime_state() -> None:
    class Result:
        def __init__(self, rows):
            self.rows = list(rows)

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class Connection:
        def execute(self, statement, _params=None):
            if "FROM pg_constraint" in statement:
                return Result(
                    [
                        (
                            "run_dispatch_producer_kind_check",
                            True,
                            "CHECK (producer_kind IN ('thread_message', 'clarification_resolution'))",
                        ),
                        (
                            "run_dispatch_request_digest_check",
                            True,
                            "CHECK (length(request_digest) = 64)",
                        ),
                        (
                            "run_dispatch_request_payload_check",
                            True,
                            "CHECK (jsonb_typeof(request_payload) = 'object')",
                        ),
                        (
                            "run_dispatch_scope_shape_check",
                            True,
                            "CHECK (scope_ref = thread_id OR scope_ref = run_id)",
                        ),
                        (
                            "run_dispatch_owner_shape_check",
                            True,
                            "CHECK (owner_id IS NOT NULL)",
                        ),
                        (
                            "run_dispatch_state_check",
                            True,
                            "CHECK (dispatch_state IN ('pending', 'leased', 'running', 'terminal'))",
                        ),
                        (
                            "run_dispatches_message_id_key",
                            True,
                            "UNIQUE (message_id)",
                        ),
                        (
                            "run_dispatches_producer_kind_scope_ref_request_identity_key",
                            True,
                            "UNIQUE (producer_kind, scope_ref, request_identity)",
                        ),
                    ]
                )
            if "information_schema.columns" in statement:
                return Result([("NO",)])
            if "FROM pg_indexes" in statement:
                return Result(
                    [
                        (
                            "run_dispatches_message_id_key",
                            "CREATE UNIQUE INDEX x ON waje_runtime.run_dispatches USING btree (message_id)",
                        ),
                        (
                            "idx_run_dispatch_one_active_per_run",
                            "CREATE UNIQUE INDEX idx_run_dispatch_one_active_per_run ON waje_runtime.run_dispatches USING btree (run_id) WHERE dispatch_state IN ('pending', 'leased', 'running')",
                        ),
                    ]
                )
            if "FROM pg_trigger" in statement:
                return Result([("O",)])
            if "duplicate_active" in statement:
                return Result([(0, 0, 0, 0, 0, 0)])
            if "FROM waje_runtime.analysis_runs" in statement:
                return Result([(0,)])
            if "FROM waje_runtime.investigation_threads" in statement:
                return Result([(0,)])
            raise AssertionError(statement)

    _validate_v8_runtime_state(Connection())
