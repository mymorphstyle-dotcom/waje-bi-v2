from __future__ import annotations

from copy import deepcopy
import inspect
import json
import re
from typing import Any, Mapping

import pytest

from bi_agent.runtime.narrative_authority import PublicClaimPalette
from bi_agent.runtime.narrative_material_persistence import (
    NarrativeMaterialPersistenceError,
    persist_narrative_material_projection,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
)
from tests.phase7.test_narrative_workflow import _authority_fixture


_SOURCE_TABLES = (
    "publication_visibility_policies",
    "public_claim_palettes",
    "public_limitations",
    "public_claims",
    "public_fact_descriptors",
    "public_recommendations",
    "narrative_material_projections",
)


def _fixture() -> dict[str, Any]:
    authority = _authority_fixture()
    execution = authority.authority_inputs.execution_result
    evidence_entries = tuple(
        entry
        for _, _, entries, _ in execution.capability_outcome_bundles
        for entry in entries
    )
    palette = PublicClaimPalette.derive(
        authority_bundle=authority.bundle,
        claims=authority.settlement.accepted_claims,
        claim_keys=authority.settlement.accepted_claim_keys,
        recommendations=authority.recommendations,
        public_facts=authority.materialization.public_facts,
        public_limitations=authority.materialization.public_limitations,
        visibility_policy=authority.policy,
    )
    projection = NarrativeMaterialProjection.derive(
        palette=palette,
        claim_settlement=authority.settlement,
        evidence_entries=evidence_entries,
    )
    return {
        "bundle": authority.bundle,
        "settlement": authority.settlement,
        "policy": authority.policy,
        "palette": palette,
        "projection": projection,
        "evidence_entries": evidence_entries,
    }


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(
        self,
        fixture: Mapping[str, Any],
        *,
        stored_owner_ref: str = "owner:narrative-material",
        fail_on_table: str | None = None,
    ) -> None:
        self.fixture = fixture
        self.stored_owner_ref = stored_owner_ref
        self.thread_ref = "thread:narrative-material"
        self.fail_on_table = fail_on_table
        self.tables: dict[str, list[dict[str, Any]]] = {
            table: [] for table in _SOURCE_TABLES
        }
        self.statements: list[tuple[str, Mapping[str, Any] | None]] = []
        self.commits = 0
        self.rollbacks = 0
        self._snapshot: dict[str, list[dict[str, Any]]] | None = None

    def execute(
        self,
        statement: str,
        params: Mapping[str, Any] | None = None,
    ) -> _Result:
        self.statements.append((statement, params))
        if "pg_advisory_xact_lock" in statement:
            self._snapshot = deepcopy(self.tables)
            return _Result([(None,)])
        if "narrative_material_checkpoint_preflight" in statement:
            return _Result(
                [
                    {
                        "owner_ref": self.stored_owner_ref,
                        "thread_ref": self.thread_ref,
                        "authority_bundle_payload": self.fixture["bundle"].to_dict(),
                        "authority_bundle_digest": self.fixture["bundle"].bundle_digest,
                        "claim_settlement_payload": self.fixture[
                            "settlement"
                        ].to_dict(),
                        "claim_settlement_digest": self.fixture[
                            "settlement"
                        ].content_digest,
                    }
                ]
            )
        insert = re.search(
            r"INSERT INTO waje_runtime\.([a-z0-9_]+) \((.*?)\)\s*VALUES",
            statement,
            re.DOTALL,
        )
        if insert is not None:
            table = insert.group(1)
            if table == self.fail_on_table:
                raise RuntimeError("injected_narrative_material_insert_failure")
            assert params is not None
            row = {
                key: json.loads(value)
                if isinstance(value, str) and key in {"payload", "public_context"}
                else value
                for key, value in params.items()
            }
            identity_columns = _identity_columns(table)
            existing = next(
                (
                    item
                    for item in self.tables[table]
                    if all(item[key] == row[key] for key in identity_columns)
                ),
                None,
            )
            if existing is not None:
                return _Result([])
            self.tables[table].append(row)
            returning = re.search(r"RETURNING ([a-z0-9_]+)", statement)
            assert returning is not None
            return _Result([{returning.group(1): row[returning.group(1)]}])
        replay = re.search(
            r"narrative_material_exact_replay:([a-z0-9_]+)",
            statement,
        )
        if replay is not None:
            table = replay.group(1)
            assert params is not None
            identity_columns = _identity_columns(table)
            row = next(
                (
                    item
                    for item in self.tables[table]
                    if all(item[key] == params[key] for key in identity_columns)
                ),
                None,
            )
            return _Result([] if row is None else [row])
        raise AssertionError(f"unexpected SQL: {statement}")

    def commit(self) -> None:
        self.commits += 1
        self._snapshot = None

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._snapshot is not None:
            self.tables = self._snapshot
        self._snapshot = None


def _identity_columns(table: str) -> tuple[str, ...]:
    if table == "publication_visibility_policies":
        return "owner_ref", "run_attempt_id", "policy_ref"
    if table == "public_limitations":
        return "owner_ref", "run_attempt_id", "palette_ref", "limitation_ref"
    return {
        "public_claim_palettes": ("palette_ref",),
        "public_claims": ("public_claim_ref",),
        "public_fact_descriptors": ("fact_ref",),
        "public_recommendations": ("public_recommendation_ref",),
        "narrative_material_projections": ("projection_ref",),
    }[table]


def _persist(connection: _Connection, fixture: Mapping[str, Any]):
    return persist_narrative_material_projection(
        connection,
        owner_ref="owner:narrative-material",
        thread_ref="thread:narrative-material",
        authority_bundle=fixture["bundle"],
        claim_settlement=fixture["settlement"],
        visibility_policy=fixture["policy"],
        palette=fixture["palette"],
        projection=fixture["projection"],
        evidence_entries=fixture["evidence_entries"],
    )


def test_checkpoint_api_has_no_provider_or_transition_dependency() -> None:
    assert tuple(
        inspect.signature(persist_narrative_material_projection).parameters
    ) == (
        "connection",
        "owner_ref",
        "thread_ref",
        "authority_bundle",
        "claim_settlement",
        "visibility_policy",
        "palette",
        "projection",
        "evidence_entries",
    )


def test_checkpoint_inserts_complete_source_chain_in_one_transaction() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)

    result = _persist(connection, fixture)

    assert result.status == "inserted"
    assert result.projection_ref == fixture["projection"].projection_ref
    assert result.palette_ref == fixture["palette"].palette_ref
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.tables["publication_visibility_policies"]
    assert connection.tables["public_claim_palettes"]
    assert connection.tables["public_limitations"]
    assert connection.tables["narrative_material_projections"]
    assert len(connection.tables["public_claims"]) == len(fixture["palette"].claims)
    assert len(connection.tables["public_fact_descriptors"]) == sum(
        len(claim.facts) for claim in fixture["palette"].claims
    )
    assert len(connection.tables["public_recommendations"]) == len(
        fixture["palette"].recommendations
    )


def test_checkpoint_exact_replay_is_pure() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    _persist(connection, fixture)
    before = deepcopy(connection.tables)

    result = _persist(connection, fixture)

    assert result.status == "replayed"
    assert connection.tables == before
    assert connection.commits == 2
    assert connection.rollbacks == 0


def test_checkpoint_tamper_rolls_back_exact_replay() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    _persist(connection, fixture)
    connection.tables["narrative_material_projections"][0]["payload"] = {
        "tampered": True
    }

    with pytest.raises(
        NarrativeMaterialPersistenceError,
        match="narrative_material_exact_replay_conflict:narrative_material_projections:payload",
    ):
        _persist(connection, fixture)

    assert connection.rollbacks == 1
    assert connection.commits == 1


def test_checkpoint_rejects_partial_chain_and_rolls_back_new_rows() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    _persist(connection, fixture)
    policy_only = deepcopy(connection.tables["publication_visibility_policies"])
    connection.tables = {table: [] for table in _SOURCE_TABLES}
    connection.tables["publication_visibility_policies"] = policy_only

    with pytest.raises(
        NarrativeMaterialPersistenceError,
        match="narrative_material_partial_checkpoint_conflict",
    ):
        _persist(connection, fixture)

    assert connection.tables["publication_visibility_policies"] == policy_only
    assert all(
        not connection.tables[table]
        for table in _SOURCE_TABLES
        if table != "publication_visibility_policies"
    )
    assert connection.rollbacks == 1


def test_checkpoint_owner_scope_conflict_rolls_back_before_insert() -> None:
    fixture = _fixture()
    connection = _Connection(fixture, stored_owner_ref="owner:other")

    with pytest.raises(
        NarrativeMaterialPersistenceError,
        match="narrative_material_owner_scope_conflict",
    ):
        _persist(connection, fixture)

    assert all(not rows for rows in connection.tables.values())
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_checkpoint_mid_transaction_failure_rolls_back_every_source_record() -> None:
    fixture = _fixture()
    connection = _Connection(fixture, fail_on_table="public_fact_descriptors")

    with pytest.raises(
        RuntimeError,
        match="injected_narrative_material_insert_failure",
    ):
        _persist(connection, fixture)

    assert all(not rows for rows in connection.tables.values())
    assert connection.commits == 0
    assert connection.rollbacks == 1
