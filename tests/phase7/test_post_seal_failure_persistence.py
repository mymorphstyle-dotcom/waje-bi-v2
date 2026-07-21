from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from bi_agent.runtime import post_seal_failure_persistence
from bi_agent.runtime.post_seal_failure_persistence import (
    PostSealFailurePersistenceError,
    load_post_seal_failure_terminal,
    record_post_seal_failure,
)
from bi_agent.runtime.single_authority import FailureRecord
from tests.phase7.test_authority_seal_persistence import _fixture


ROOT = Path(__file__).resolve().parents[2]


def _sealed_lifecycle(fixture: Any):
    return fixture.lifecycle.transition(
        evidence_state=(
            "boundary_only"
            if fixture.bundle.authority_mode == "boundary_only"
            else "complete"
        )
    )


class _Cursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class _Connection:
    def __init__(self, fixture: Any) -> None:
        self.fixture = fixture
        self.lifecycle_payload = _sealed_lifecycle(fixture).to_dict()
        self.transition_payload = fixture.settlement_transition.to_dict()
        self.failure_payload: Mapping[str, Any] | None = None
        self.terminal_payload: Mapping[str, Any] | None = None
        self.terminal_payloads: list[Mapping[str, Any]] = []
        self.audit_payloads: list[Mapping[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(
        self,
        statement: str,
        params: Mapping[str, Any] | None = None,
    ) -> _Cursor:
        params = dict(params or {})
        if "pg_advisory_xact_lock" in statement:
            return _Cursor([(1,)])
        if "post_seal_failure_preflight" in statement:
            return _Cursor(
                [
                    {
                        "thread_id": "thread:authority-seal",
                        "owner_ref": "owner:authority-seal",
                        "authority_bundle_payload": self.fixture.bundle.to_dict(),
                        "bundle_digest": self.fixture.bundle.bundle_digest,
                        "authority_transition_payload": self.transition_payload,
                        "lifecycle_payload": self.lifecycle_payload,
                        "existing_terminal_payload": self.terminal_payload,
                    }
                ]
            )
        if "load_post_seal_failure_terminal" in statement:
            return _Cursor(
                [] if self.terminal_payload is None else [(self.terminal_payload,)]
            )
        if statement.lstrip().startswith("INSERT INTO waje_runtime.failure_records"):
            self.failure_payload = params["payload"]
            return _Cursor([(params["failure_id"],)])
        if statement.lstrip().startswith(
            "INSERT INTO waje_runtime.run_lifecycle_state_revisions"
        ):
            self.lifecycle_payload = params["payload"]
            return _Cursor([(params["state_revision"],)])
        if statement.lstrip().startswith(
            "INSERT INTO waje_runtime.post_seal_failure_terminals"
        ):
            self.terminal_payload = json.loads(params["payload"])
            self.terminal_payloads.append(self.terminal_payload)
            return _Cursor([(params["terminal_ref"],)])
        if statement.lstrip().startswith("INSERT INTO waje_runtime.audit_events"):
            self.audit_payloads.append(json.loads(params["payload"]))
            return _Cursor([(1,)])
        if "FROM waje_runtime.audit_events" in statement:
            return _Cursor(
                [
                    (item,)
                    for item in self.audit_payloads
                    if item["terminal_ref"] == params["terminal_ref"]
                ]
            )
        raise AssertionError(f"unexpected SQL: {statement}")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _failure(fixture: Any, *, kind: str = "provider_rate_limited") -> FailureRecord:
    return FailureRecord.create(
        layer="narrative",
        kind=kind,
        scope="run",
        affected_refs=(
            fixture.bundle.bundle_ref,
            fixture.settlement_transition.transition_id,
            "narrative-provider-input:sha256:" + "8" * 64,
        ),
        integrity_level="local",
        retryability="retryable",
        user_actionable=False,
        business_boundary="Accepted analysis retained; publication unavailable.",
        technical_detail_ref="technical-detail:sha256:" + "9" * 64,
    )


def _record(
    connection: _Connection,
    fixture: Any,
    failure: FailureRecord,
    *,
    supersedes_terminal_ref: str | None = None,
):
    return record_post_seal_failure(
        connection,
        owner_ref="owner:authority-seal",
        thread_ref="thread:authority-seal",
        authority_bundle=fixture.bundle,
        authority_transition=fixture.settlement_transition,
        status="narrative_failed",
        failure_record=failure,
        supersedes_terminal_ref=supersedes_terminal_ref,
    )


def test_post_seal_failure_record_is_atomic_and_exactly_replayable() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    failure = _failure(fixture)

    inserted = _record(connection, fixture, failure)
    replayed = _record(connection, fixture, failure)
    loaded = load_post_seal_failure_terminal(
        connection,
        authority_bundle=fixture.bundle,
        authority_transition=fixture.settlement_transition,
    )

    assert inserted.status == "inserted"
    assert replayed.status == "replayed"
    assert replayed.terminal == inserted.terminal == loaded
    assert inserted.terminal.failure_record == failure
    assert inserted.terminal.lifecycle_state.prior_state_digest == (
        _sealed_lifecycle(fixture).content_digest
    )
    assert inserted.terminal.lifecycle_state.retry_state == "exhausted"
    assert inserted.terminal.lifecycle_state.evidence_state == "complete"
    assert len(connection.audit_payloads) == 1
    assert connection.commits == 2
    assert connection.rollbacks == 0


def test_retryable_failure_appends_attempt_chain_and_replays_exact_retry() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    first = _record(connection, fixture, _failure(fixture))

    second = _record(
        connection,
        fixture,
        _failure(fixture, kind="provider_unavailable"),
        supersedes_terminal_ref=first.terminal.terminal_ref,
    )
    replayed = _record(
        connection,
        fixture,
        _failure(fixture, kind="provider_unavailable"),
        supersedes_terminal_ref=first.terminal.terminal_ref,
    )

    assert second.status == "inserted"
    assert second.terminal.attempt_number == 2
    assert second.terminal.supersedes_terminal_ref == first.terminal.terminal_ref
    assert second.terminal.authority_bundle_ref == first.terminal.authority_bundle_ref
    assert replayed.status == "replayed"
    assert replayed.terminal == second.terminal
    assert [payload["terminal_ref"] for payload in connection.terminal_payloads] == [
        first.terminal.terminal_ref,
        second.terminal.terminal_ref,
    ]
    assert len(connection.audit_payloads) == 2


def test_post_seal_failure_replay_rejects_a_different_failure() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    _record(connection, fixture, _failure(fixture))

    with pytest.raises(
        PostSealFailurePersistenceError,
        match="post_seal_failure_terminal_replay_conflict",
    ):
        _record(
            connection,
            fixture,
            _failure(fixture, kind="provider_unavailable"),
        )

    assert len(connection.audit_payloads) == 1
    assert connection.rollbacks == 1


def test_post_seal_failure_rejects_non_exact_typed_authority_transition() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    connection.transition_payload = {
        **fixture.settlement_transition.to_dict(),
        "provider_ref": "provider:other",
    }

    with pytest.raises(
        PostSealFailurePersistenceError,
        match="post_seal_failure_authority_conflict",
    ):
        _record(connection, fixture, _failure(fixture))

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_post_seal_failure_schema_is_append_only_and_run_scoped() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text()

    assert (
        "CREATE TABLE IF NOT EXISTS waje_runtime.post_seal_failure_terminals" in schema
    )
    assert "UNIQUE(run_attempt_id, attempt_number)" in schema
    assert "FOREIGN KEY (run_attempt_id, supersedes_terminal_ref)" in schema
    assert "'post_seal_failure_terminals'" in schema


def test_post_seal_failure_preflight_reconstructs_typed_transition_from_columns() -> (
    None
):
    sql = post_seal_failure_persistence._PREFLIGHT_SQL

    assert "transition.payload AS authority_transition_payload" not in sql
    assert (
        """to_jsonb(transition)
    - 'input_payload'
    - 'output_payload'
    - 'failure_ref'
    - 'created_at' AS authority_transition_payload"""
        in sql
    )
