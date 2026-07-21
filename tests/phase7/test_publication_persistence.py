from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import inspect
import json
import re
from typing import Any, Mapping

import pytest

import bi_agent.runtime.publication_persistence as persistence
from bi_agent.runtime.durable_call_journal import (
    DurableCallSpec,
    InMemoryDurableCallJournal,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.narrative_workflow import (
    NarrativeWorkflowResult,
    prepare_narrative_material_projection,
    run_narrative_workflow,
)
from bi_agent.runtime.publication_flow import PublicationFlowResult
from bi_agent.runtime.publication_persistence import (
    DeliveryTransportResult,
    PublicationPersistenceError,
    deliver_persisted_outbox,
    narrative_publication_transition_payloads,
    persist_publication,
)
from bi_agent.runtime.single_authority import DurableTransition, LifecycleState
from tests.phase7.test_narrative_workflow import (
    _FakeNarrativeLLM,
    _NoSensitiveOutput,
    _authority_fixture,
    _context,
    _focused_result,
    _focused_writer,
    _initial_writer,
    _veto_role,
)


@dataclass(frozen=True)
class _Fixture:
    authority_inputs: Any
    bundle: Any
    settlement: Any
    recommendations: tuple[Any, ...]
    workflow: NarrativeWorkflowResult
    flow: PublicationFlowResult | None
    parent_transition: DurableTransition
    parent_input: Mapping[str, Any]
    parent_output: Mapping[str, Any]
    compose_transition: DurableTransition
    lifecycle: LifecycleState


def _fixture(*, withheld: bool = False) -> _Fixture:
    if withheld:
        authority = _authority_fixture()
        _, material_projection = prepare_narrative_material_projection(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
        )
        client = _FakeNarrativeLLM(
            (
                _initial_writer,
                _veto_role("dimension_localization"),
                _focused_writer,
                _veto_role("dimension_localization"),
            ),
            retry_audit_calls=(0, 2),
        )
        workflow = run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=material_projection,
            answer_context=_context(),
            llm_client=client,
            sensitive_output_inspector=_NoSensitiveOutput(),
        )
    else:
        authority, _, workflow = _focused_result()
    flow = (
        None
        if withheld
        else PublicationFlowResult.create(
            authority_inputs=authority.authority_inputs,
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            recommendations=authority.recommendations,
            narrative_workflow=workflow,
            supersedes_publication=None,
            destination_ref="conversation:phase6-tests",
            channel="conversation",
            published_at="2026-07-18T12:30:00Z",
        )
    )
    parent_input = {
        "authoritative_execution_result_ref": authority.bundle.execution_result_ref,
        "authoritative_execution_result_digest": (
            authority.bundle.execution_result_digest
        ),
    }
    parent_output = {
        "semantic_authority_result": {
            "claim_settlement_ref": authority.settlement.settlement_ref,
            "claim_settlement_digest": authority.settlement.content_digest,
        },
        "authority_bundle": authority.bundle.to_dict(),
    }
    parent = DurableTransition.create(
        node_name="settle_claim_authority",
        parent_transition_id="transition:execute-capability-dag",
        run_attempt_id=authority.bundle.run_attempt_id,
        intent_revision_id=authority.bundle.intent_revision_id,
        decision_ledger_position=3,
        input_digest=canonical_digest(parent_input),
        output_digest=canonical_digest(parent_output),
        execution_attempt=1,
        provider_ref="waje-semantic-authority",
        model_ref="single-authority-phase04.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="compose_claim_aware_narrative",
        started_at="2026-07-18T12:00:00Z",
        finished_at="2026-07-18T12:01:00Z",
    )
    transition_input, transition_output = narrative_publication_transition_payloads(
        authority_inputs=authority.authority_inputs,
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        recommendations=authority.recommendations,
        narrative_workflow=workflow,
        publication_flow=flow,
        supersedes_publication=None,
    )
    compose = DurableTransition.create(
        node_name="compose_claim_aware_narrative",
        parent_transition_id=parent.transition_id,
        run_attempt_id=authority.bundle.run_attempt_id,
        intent_revision_id=authority.bundle.intent_revision_id,
        decision_ledger_position=parent.decision_ledger_position,
        input_digest=canonical_digest(transition_input),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref="waje-narrative-authority",
        model_ref="single-authority-phase05.v9",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="deliver_publication",
        started_at="2026-07-18T12:01:00Z",
        finished_at="2026-07-18T12:02:00Z",
    )
    lifecycle = LifecycleState.create(
        run_attempt_id=authority.bundle.run_attempt_id,
        execution_state="complete",
        evidence_state=(
            "boundary_only"
            if authority.bundle.authority_mode == "boundary_only"
            else "complete"
        ),
        publication_state="not_ready",
        delivery_state="pending",
    )
    return _Fixture(
        authority_inputs=authority.authority_inputs,
        bundle=authority.bundle,
        settlement=authority.settlement,
        recommendations=authority.recommendations,
        workflow=workflow,
        flow=flow,
        parent_transition=parent,
        parent_input=parent_input,
        parent_output=parent_output,
        compose_transition=compose,
        lifecycle=lifecycle,
    )


class _Cursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(
        self,
        fixture: _Fixture,
        *,
        bundle_payload_override: Mapping[str, Any] | None = None,
        parent_payload_override: Mapping[str, Any] | None = None,
        material_projection_payload_override: Mapping[str, Any] | None = None,
        fail_on_table: str | None = None,
    ) -> None:
        self.fixture = fixture
        self.owner_ref = "owner:semantic-authority"
        self.thread_ref = "thread:semantic-authority"
        self.bundle_payload_override = bundle_payload_override
        self.parent_payload_override = parent_payload_override
        self.material_projection_payload_override = material_projection_payload_override
        self.fail_on_table = fail_on_table
        self.lifecycle = fixture.lifecycle
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.statements: list[tuple[str, Mapping[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.attempt_journal = InMemoryDurableCallJournal()
        self._snapshot: (
            tuple[dict[str, list[dict[str, Any]]], LifecycleState] | None
        ) = None

    def execute(self, statement: str, params: Mapping[str, Any] | None = None):
        normalized_params = dict(params or {})
        self.statements.append((statement, normalized_params))
        if "pg_advisory_xact_lock" in statement:
            self._snapshot = (deepcopy(self.tables), self.lifecycle)
            return _Cursor([(1,)])
        if "publication_persistence_preflight" in statement:
            compose_row = self._by(
                "workflow_transition_attempts",
                attempt_id=normalized_params["compose_attempt_id"],
            )
            publication = self._by(
                "publication_revisions",
                publication_ref=normalized_params["publication_ref"],
            )
            latest = self._latest_publication()
            outbox = self._by(
                "delivery_outbox_records",
                outbox_ref=normalized_params["outbox_ref"],
            )
            customer = self._by(
                "publication_customer_payloads",
                outbox_ref=normalized_params["outbox_ref"],
            )
            return _Cursor(
                [
                    {
                        "owner_ref": self.owner_ref,
                        "thread_ref": self.thread_ref,
                        "authority_bundle_payload": (
                            self.bundle_payload_override
                            or self.fixture.bundle.to_dict()
                        ),
                        "authority_bundle_digest": self.fixture.bundle.bundle_digest,
                        "parent_transition_payload": (
                            self.parent_payload_override
                            or self.fixture.parent_transition.to_dict()
                        ),
                        "parent_transition_input_payload": self.fixture.parent_input,
                        "parent_transition_output_payload": self.fixture.parent_output,
                        "lifecycle_payload": self.lifecycle.to_dict(),
                        "existing_transition_payload": self._transition_payload(
                            compose_row
                        ),
                        "existing_transition_input_payload": self._value(
                            compose_row, "input_payload"
                        ),
                        "existing_transition_output_payload": self._value(
                            compose_row, "output_payload"
                        ),
                        "expected_publication_payload": self._payload(publication),
                        "latest_publication_payload": self._payload(latest),
                        "expected_outbox_payload": self._payload(outbox),
                        "expected_customer_payload": self._payload(customer),
                        "material_projection_payload": (
                            self.material_projection_payload_override
                            or self.fixture.workflow.material_projection.to_dict()
                        ),
                        "material_projection_digest": (
                            self.fixture.workflow.material_projection.content_digest
                        ),
                        "material_projection_palette_ref": (
                            self.fixture.workflow.material_projection.palette_ref
                        ),
                        "material_projection_palette_digest": (
                            self.fixture.workflow.material_projection.palette_digest
                        ),
                        "material_projection_settlement_ref": (
                            self.fixture.workflow.material_projection.claim_settlement_ref
                        ),
                        "material_projection_settlement_digest": (
                            self.fixture.workflow.material_projection.claim_settlement_digest
                        ),
                    }
                ]
            )
        if "delivery_persistence_scope" in statement:
            row = self._by(
                "delivery_outbox_records",
                outbox_ref=normalized_params["outbox_ref"],
            )
            return _Cursor([] if row is None else [(row["run_attempt_id"],)])
        if "delivery_persistence_preflight" in statement:
            outbox = self._by(
                "delivery_outbox_records",
                outbox_ref=normalized_params["outbox_ref"],
            )
            if outbox is None:
                return _Cursor([])
            customer = self._by(
                "publication_customer_payloads",
                outbox_ref=outbox["outbox_ref"],
            )
            publication = self._by(
                "publication_revisions",
                publication_ref=outbox["publication_ref"],
            )
            customer_publication = self._by(
                "customer_publications",
                outbox_ref=outbox["outbox_ref"],
            )
            return _Cursor(
                [
                    {
                        "owner_ref": outbox["owner_ref"],
                        "run_attempt_id": outbox["run_attempt_id"],
                        "outbox_payload": outbox["payload"],
                        "customer_payload_record": customer["payload"],
                        "lifecycle_payload": self.lifecycle.to_dict(),
                        "publication_payload": publication["payload"],
                        "authority_bundle_payload": self.fixture.bundle.to_dict(),
                        "customer_publication_payload": self._payload(
                            customer_publication
                        ),
                    }
                ]
            )
        if "delivery_attempt_history" in statement:
            rows = sorted(
                (
                    row
                    for row in self.tables.get("delivery_attempts", [])
                    if row["outbox_ref"] == normalized_params["outbox_ref"]
                ),
                key=lambda row: row["attempt_number"],
            )
            return _Cursor([(row["payload"],) for row in rows])
        if "publication_exact_replay:" in statement:
            table = statement.split("publication_exact_replay:", 1)[1].split(" */", 1)[
                0
            ]
            row = self._by(table, **normalized_params)
            if row is None:
                return _Cursor([])
            columns = statement.split("SELECT", 1)[1].split("FROM", 1)[0]
            names = tuple(item.strip() for item in columns.split(","))
            return _Cursor([tuple(row[name] for name in names)])
        if statement.lstrip().startswith("INSERT INTO waje_runtime."):
            return self._insert(statement, normalized_params)
        raise AssertionError(f"unexpected SQL: {statement}")

    def _insert(self, statement: str, params: Mapping[str, Any]) -> _Cursor:
        table = statement.split("INSERT INTO waje_runtime.", 1)[1].split()[0]
        if table == self.fail_on_table:
            raise RuntimeError(f"injected_insert_failure:{table}")
        columns_text = statement.split("(", 1)[1].split(")", 1)[0]
        columns = tuple(item.strip() for item in columns_text.split(","))
        conflict_text = statement.split("ON CONFLICT (", 1)[1].split(")", 1)[0]
        conflict_columns = tuple(item.strip() for item in conflict_text.split(","))
        returning = statement.split("RETURNING", 1)[1].strip().split()[0]
        json_columns = {name for name in columns if f"%({name})s::jsonb" in statement}
        decoded = {
            name: self._decode(params[name]) if name in json_columns else params[name]
            for name in columns
        }
        existing = self._by(
            table,
            **{name: decoded[name] for name in conflict_columns},
        )
        if existing is not None:
            return _Cursor([])
        self.tables.setdefault(table, []).append(decoded)
        if table == "run_lifecycle_state_revisions":
            self.lifecycle = LifecycleState.from_dict(decoded["payload"])
        return _Cursor([(decoded[returning],)])

    @staticmethod
    def _decode(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def _by(self, table: str, **identity: Any) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.tables.get(table, [])
                if all(row.get(key) == value for key, value in identity.items())
            ),
            None,
        )

    def _latest_publication(self) -> dict[str, Any] | None:
        rows = self.tables.get("publication_revisions", [])
        return max(rows, key=lambda row: row["revision"]) if rows else None

    @staticmethod
    def _payload(row: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        return None if row is None else row["payload"]

    @staticmethod
    def _value(row: Mapping[str, Any] | None, key: str) -> Any:
        return None if row is None else row[key]

    @staticmethod
    def _transition_payload(
        row: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in DurableTransition.__dataclass_fields__}

    def commit(self) -> None:
        self.commits += 1
        self._snapshot = None

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._snapshot is not None:
            self.tables, self.lifecycle = self._snapshot
        self._snapshot = None


def _persist(connection: _Connection):
    fixture = connection.fixture
    assert fixture.flow is not None
    attempt_refs = _narrative_attempt_refs(connection)
    return persist_publication(
        connection,
        owner_ref=connection.owner_ref,
        thread_ref=connection.thread_ref,
        authority_inputs=fixture.authority_inputs,
        authority_bundle=fixture.bundle,
        claim_settlement=fixture.settlement,
        recommendations=fixture.recommendations,
        narrative_workflow=fixture.workflow,
        publication_flow=fixture.flow,
        supersedes_publication=None,
        compose_transition=fixture.compose_transition,
        attempt_journal=connection.attempt_journal,
        accepted_attempt_refs=attempt_refs,
    )


def _persist_withheld(connection: _Connection):
    fixture = connection.fixture
    attempt_refs = _narrative_attempt_refs(connection)
    return persistence.persist_withheld_publication(
        connection,
        owner_ref=connection.owner_ref,
        thread_ref=connection.thread_ref,
        authority_inputs=fixture.authority_inputs,
        authority_bundle=fixture.bundle,
        claim_settlement=fixture.settlement,
        recommendations=fixture.recommendations,
        narrative_workflow=fixture.workflow,
        compose_transition=fixture.compose_transition,
        attempt_journal=connection.attempt_journal,
        accepted_attempt_refs=attempt_refs,
    )


def _narrative_attempt_refs(connection: _Connection) -> tuple[str, ...]:
    fixture = connection.fixture
    refs: list[str] = []
    for index, response in enumerate(fixture.workflow.provider_responses):
        input_payload = {
            "test_narrative_provider_call": response.purpose,
            "index": index,
        }
        input_digest = canonical_digest(input_payload)
        spec = DurableCallSpec.create(
            run_attempt_id=fixture.bundle.run_attempt_id,
            intent_revision_id=fixture.bundle.intent_revision_id,
            plan_revision_id=fixture.bundle.plan_revision_id,
            task_id=None,
            stage_name="compose_claim_aware_narrative",
            call_kind="narrative_provider",
            operation_name=f"test_{response.purpose}_{index}",
            input_ref="provider-call-input:sha256:" + input_digest,
            input_payload=input_payload,
        )
        claim = connection.attempt_journal.claim(spec)
        if claim.replayed:
            refs.append(claim.attempt.attempt_ref)
            continue
        completion = connection.attempt_journal.succeed(
            claim.attempt,
            {"output": {"accepted": True}, "audit": {"index": index}},
        )
        assert completion.acceptance is not None
        refs.append(completion.acceptance.accepted_attempt_ref)
    return tuple(refs)


def test_publication_flow_roundtrip_and_nested_tamper() -> None:
    fixture = _fixture()
    assert fixture.flow is not None

    replayed = PublicationFlowResult.from_dict(
        fixture.flow.to_dict(),
        authority_inputs=fixture.authority_inputs,
        authority_bundle=fixture.bundle,
        claim_settlement=fixture.settlement,
        recommendations=fixture.recommendations,
        narrative_workflow=fixture.workflow,
        supersedes_publication=None,
    )

    assert replayed == fixture.flow
    tampered = deepcopy(fixture.flow.to_dict())
    tampered["projection"]["projection_digest"] = "0" * 64
    with pytest.raises(ValueError):
        PublicationFlowResult.from_dict(
            tampered,
            authority_inputs=fixture.authority_inputs,
            authority_bundle=fixture.bundle,
            claim_settlement=fixture.settlement,
            recommendations=fixture.recommendations,
            narrative_workflow=fixture.workflow,
            supersedes_publication=None,
        )


def _without_required_claim(payload: Mapping[str, Any]) -> dict[str, Any]:
    missing = deepcopy(payload)
    missing["claim_refs"] = []
    for block in missing["blocks"]:
        block["claim_refs"] = []
    return missing


def test_persistence_rejects_customer_payload_mutated_after_projection() -> None:
    fixture = _fixture()
    assert fixture.flow is not None
    connection = _Connection(fixture)
    invalid_flow = replace(
        fixture.flow,
        customer_payload=_without_required_claim(fixture.flow.customer_payload),
    )

    with pytest.raises(PublicationPersistenceError, match="publication_flow_invalid"):
        persist_publication(
            connection,
            owner_ref=connection.owner_ref,
            thread_ref=connection.thread_ref,
            authority_inputs=fixture.authority_inputs,
            authority_bundle=fixture.bundle,
            claim_settlement=fixture.settlement,
            recommendations=fixture.recommendations,
            narrative_workflow=fixture.workflow,
            publication_flow=invalid_flow,
            supersedes_publication=None,
            compose_transition=fixture.compose_transition,
            attempt_journal=connection.attempt_journal,
            accepted_attempt_refs=(),
        )

    assert connection.statements == []


@pytest.mark.parametrize("child", ("projection", "publication", "outbox"))
def test_persistence_rejects_tampered_nested_publication_flow(child: str) -> None:
    fixture = _fixture()
    assert fixture.flow is not None
    connection = _Connection(fixture)
    if child == "projection":
        invalid_flow = replace(
            fixture.flow,
            projection=replace(fixture.flow.projection, projection_digest="0" * 64),
        )
    elif child == "publication":
        invalid_flow = replace(
            fixture.flow,
            publication=replace(fixture.flow.publication, publication_digest="0" * 64),
        )
    else:
        invalid_flow = replace(
            fixture.flow,
            outbox=replace(fixture.flow.outbox, content_digest="0" * 64),
        )

    with pytest.raises(PublicationPersistenceError, match="publication_flow_invalid"):
        persist_publication(
            connection,
            owner_ref=connection.owner_ref,
            thread_ref=connection.thread_ref,
            authority_inputs=fixture.authority_inputs,
            authority_bundle=fixture.bundle,
            claim_settlement=fixture.settlement,
            recommendations=fixture.recommendations,
            narrative_workflow=fixture.workflow,
            publication_flow=invalid_flow,
            supersedes_publication=None,
            compose_transition=fixture.compose_transition,
            attempt_journal=connection.attempt_journal,
            accepted_attempt_refs=(),
        )

    assert connection.statements == []


def test_persistence_validates_context_and_flow_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    calls = {"context": 0, "flow": 0}
    original_context = persistence.validate_publication_flow_context
    original_flow = persistence.validate_publication_flow_in_context

    def counted_context(**kwargs: Any):
        calls["context"] += 1
        return original_context(**kwargs)

    def counted_flow(value: Any, **kwargs: Any):
        calls["flow"] += 1
        return original_flow(value, **kwargs)

    monkeypatch.setattr(
        persistence,
        "validate_publication_flow_context",
        counted_context,
    )
    monkeypatch.setattr(
        persistence,
        "validate_publication_flow_in_context",
        counted_flow,
    )

    _persist(connection)

    assert calls == {"context": 1, "flow": 1}


def test_persistence_api_accepts_only_full_workflow_and_publication_flow() -> None:
    signature = inspect.signature(persist_publication)
    assert tuple(signature.parameters) == (
        "connection",
        "owner_ref",
        "thread_ref",
        "authority_inputs",
        "authority_bundle",
        "claim_settlement",
        "recommendations",
        "narrative_workflow",
        "publication_flow",
        "supersedes_publication",
        "compose_transition",
        "attempt_journal",
        "accepted_attempt_refs",
    )
    assert not {
        "palette",
        "narrative",
        "local_report",
        "verifier_report",
        "projection",
        "outbox",
        "customer_payload",
    }.intersection(signature.parameters)


def test_transaction_persists_single_generation_and_background_review_chain() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)

    result = _persist(connection)

    assert result.status == "inserted"
    assert result.publication_state == "ready"
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(
        connection.attempt_journal.load_stage_attempt_refs(
            run_attempt_id=fixture.bundle.run_attempt_id,
            transition_attempt_id=fixture.compose_transition.attempt_id,
            stage_name="compose_claim_aware_narrative",
        )
    ) == len(fixture.workflow.provider_responses)
    assert len(connection.tables["restricted_provider_responses"]) == 3
    assert len(connection.tables["narrative_writer_attempts"]) == 1
    assert len(connection.tables["narrative_documents"]) == 1
    assert len(connection.tables["narrative_blocks"]) == 2
    assert len(connection.tables["block_local_validation_reports"]) == 1
    assert len(connection.tables["block_verification_attempts"]) == 1
    assert len(connection.tables["block_verification_reports"]) == 1
    assert all(
        row["raw_response_content"]
        for row in connection.tables["restricted_provider_responses"]
    )
    checkpoint_owned_tables = {
        "publication_visibility_policies",
        "public_claim_palettes",
        "public_limitations",
        "public_claims",
        "public_fact_descriptors",
        "public_recommendations",
        "narrative_material_projections",
    }
    assert checkpoint_owned_tables.isdisjoint(connection.tables)
    material_projection = fixture.workflow.material_projection
    assert all(
        row["material_projection_ref"] == material_projection.projection_ref
        for row in connection.tables["narrative_writer_attempts"]
    )
    assert all(
        row["material_projection_ref"] == material_projection.projection_ref
        for row in connection.tables["narrative_documents"]
    )
    (source_narrative,) = fixture.workflow.narratives
    document_rows = {
        row["narrative_id"]: row for row in connection.tables["narrative_documents"]
    }
    assert document_rows[source_narrative.narrative_id]["parent_narrative_id"] is None
    assert all(
        "focused_retry_of_block_id" not in row and "focused_retry_report_ref" not in row
        for row in document_rows.values()
    )
    assert len(document_rows[source_narrative.narrative_id]["payload"]["blocks"]) == 2
    assert all(
        row["material_projection_ref"] == material_projection.projection_ref
        and row["material_projection_digest"] == material_projection.content_digest
        for row in connection.tables["block_local_validation_reports"]
    )
    assert all(
        row["material_projection_ref"] == material_projection.projection_ref
        and row["material_projection_digest"] == material_projection.content_digest
        for row in connection.tables["publication_projections"]
    )
    assert fixture.flow is not None
    publication = connection.tables["publication_revisions"][0]
    assert publication["narrative_id"] == (
        fixture.workflow.final_accepted_narrative.narrative_id
    )
    assert publication["local_report_ref"] == (
        fixture.workflow.final_local_report.local_report_ref
    )
    assert publication["block_verifier_report_ref"] == (
        fixture.workflow.projection_ready_verifier_report.verifier_report_ref
    )
    transition = connection.tables["workflow_transition_attempts"][0]
    assert transition["node_name"] == "compose_claim_aware_narrative"
    assert transition["parent_transition_id"] == fixture.parent_transition.transition_id
    assert transition["next_transition"] == "deliver_publication"
    output = transition["output_payload"]
    replayed_workflow = NarrativeWorkflowResult.from_dict(
        output["narrative_workflow_result"],
        authority_bundle=fixture.bundle,
        claim_settlement=fixture.settlement,
        evidence_entries=fixture.authority_inputs.material_projection_evidence_entries(),
        recommendations=fixture.recommendations,
    )
    replayed_flow = PublicationFlowResult.from_dict(
        output["publication_flow"],
        authority_inputs=fixture.authority_inputs,
        authority_bundle=fixture.bundle,
        claim_settlement=fixture.settlement,
        recommendations=fixture.recommendations,
        narrative_workflow=replayed_workflow,
        supersedes_publication=None,
    )
    assert replayed_workflow == fixture.workflow
    assert replayed_flow == fixture.flow
    assert connection.lifecycle.publication_state == "ready"
    assert connection.lifecycle.delivery_state == "persisted"


def test_exact_replay_compares_full_chain_and_transition() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    _persist(connection)
    counts = {table: len(rows) for table, rows in connection.tables.items()}

    replayed = _persist(connection)

    assert replayed.status == "replayed"
    assert {table: len(rows) for table, rows in connection.tables.items()} == counts
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "publication_exact_replay:restricted_provider_responses" in sql
    assert "publication_exact_replay:workflow_transition_attempts" in sql
    assert "DO UPDATE" not in sql


def test_raw_provider_response_tamper_rolls_back_replay() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    _persist(connection)
    response = connection.tables["restricted_provider_responses"][0]
    response["raw_response_content"] = '{"tampered":true}'

    with pytest.raises(ValueError, match="publication_exact_replay_conflict"):
        _persist(connection)

    assert connection.rollbacks == 1
    assert connection.commits == 1


def test_mid_transaction_failure_rolls_back_every_attempt_and_transition() -> None:
    fixture = _fixture()
    connection = _Connection(fixture, fail_on_table="block_verification_reports")

    with pytest.raises(RuntimeError, match="injected_insert_failure"):
        _persist(connection)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.tables == {}
    assert connection.lifecycle == fixture.lifecycle


def test_parent_transition_and_decision_position_are_hard_boundaries() -> None:
    fixture = _fixture()
    wrong_parent = fixture.parent_transition.to_dict()
    wrong_parent["next_transition"] = "unexpected"
    connection = _Connection(fixture, parent_payload_override=wrong_parent)

    with pytest.raises(ValueError, match="publication_parent_transition_invalid"):
        _persist(connection)

    input_payload, output_payload = narrative_publication_transition_payloads(
        authority_inputs=fixture.authority_inputs,
        authority_bundle=fixture.bundle,
        claim_settlement=fixture.settlement,
        recommendations=fixture.recommendations,
        narrative_workflow=fixture.workflow,
        publication_flow=fixture.flow,
        supersedes_publication=None,
    )
    compose_payload = fixture.compose_transition.to_dict()
    wrong_position = DurableTransition.create(
        **{
            key: value
            for key, value in compose_payload.items()
            if key
            not in {
                "attempt_id",
                "transition_id",
                "decision_ledger_position",
                "input_digest",
                "output_digest",
            }
        },
        decision_ledger_position=4,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
    )
    wrong_fixture = replace(fixture, compose_transition=wrong_position)
    wrong_connection = _Connection(wrong_fixture)
    with pytest.raises(ValueError, match="publication_parent_transition_invalid"):
        _persist(wrong_connection)


def _obsolete_withheld_path_persists_full_attempt_chain_without_publication_artifacts() -> (
    None
):
    fixture = _fixture(withheld=True)
    connection = _Connection(fixture)

    result = _persist_withheld(connection)

    assert result.status == "inserted"
    assert result.publication_state == "withheld"
    assert result.publication_ref is None
    assert result.outbox_ref is None
    assert result.customer_payload_ref is None
    assert len(connection.tables["restricted_provider_responses"]) == 6
    assert len(connection.tables["narrative_documents"]) == 2
    assert len(connection.tables["block_verification_reports"]) == 2
    assert "publication_projections" not in connection.tables
    assert "publication_revisions" not in connection.tables
    assert "delivery_outbox_records" not in connection.tables
    assert "publication_customer_payloads" not in connection.tables
    transition = connection.tables["workflow_transition_attempts"][0]
    assert transition["output_payload"]["publication_flow"] is None
    assert transition["output_payload"]["publication_state"] == "withheld"
    replayed = NarrativeWorkflowResult.from_dict(
        transition["output_payload"]["narrative_workflow_result"],
        authority_bundle=fixture.bundle,
        claim_settlement=fixture.settlement,
        evidence_entries=fixture.authority_inputs.material_projection_evidence_entries(),
        recommendations=fixture.recommendations,
    )
    assert replayed == fixture.workflow
    assert connection.lifecycle.publication_state == "withheld"
    assert connection.lifecycle.delivery_state == "pending"


def _obsolete_withheld_path_replays_without_creating_fallback_artifacts() -> None:
    fixture = _fixture(withheld=True)
    connection = _Connection(fixture)
    _persist_withheld(connection)

    result = _persist_withheld(connection)

    assert result.status == "replayed"
    assert "publication_revisions" not in connection.tables
    assert len(connection.tables["workflow_transition_attempts"]) == 1


@pytest.mark.parametrize("withheld", (False,))
def test_successful_post_seal_retry_marks_lifecycle_succeeded(withheld: bool) -> None:
    fixture = _fixture(withheld=withheld)
    connection = _Connection(fixture)
    connection.lifecycle = fixture.lifecycle.transition(retry_state="exhausted")

    result = _persist_withheld(connection) if withheld else _persist(connection)

    assert result.status == "inserted"
    assert connection.lifecycle.retry_state == "succeeded"
    assert connection.lifecycle.prior_state_digest is not None


def test_bundle_digest_conflict_fails_before_publication_write() -> None:
    fixture = _fixture()
    conflicting = fixture.bundle.to_dict()
    conflicting["bundle_digest"] = "0" * 64
    connection = _Connection(fixture, bundle_payload_override=conflicting)

    with pytest.raises(ValueError, match="publication_authority_bundle_conflict"):
        _persist(connection)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not connection.tables


def test_material_projection_checkpoint_conflict_fails_before_publication_write() -> (
    None
):
    fixture = _fixture()
    conflicting = fixture.workflow.material_projection.to_dict()
    conflicting["answer_contract_ref"] = "answer-contract:tampered"
    connection = _Connection(
        fixture,
        material_projection_payload_override=conflicting,
    )

    with pytest.raises(
        PublicationPersistenceError,
        match="publication_material_projection_conflict",
    ):
        _persist(connection)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not connection.tables


def test_retryable_delivery_failure_preserves_publication_then_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    connection = _Connection(fixture)
    _persist(connection)
    assert fixture.flow is not None
    publication_payload = connection.tables["publication_revisions"][0]["payload"]
    timestamps = iter(("2026-07-18T13:00:00Z", "2026-07-18T13:01:00Z"))
    monkeypatch.setattr(persistence, "_utc_now", lambda: next(timestamps))
    delivered_messages = []

    failed = deliver_persisted_outbox(
        connection,
        outbox_ref=fixture.flow.outbox.outbox_ref,
        transport=lambda message: (
            delivered_messages.append(message)
            or DeliveryTransportResult.failed(
                retryable=True,
                failure_code="transport_unavailable",
            )
        ),
    )

    assert failed.status == "retryable_failed"
    assert connection.lifecycle.publication_state == "ready"
    assert connection.lifecycle.delivery_state == "retryable_failed"
    assert connection.tables["publication_revisions"][0]["payload"] == (
        publication_payload
    )
    assert "customer_publications" not in connection.tables
    assert delivered_messages[0].customer_payload

    published = deliver_persisted_outbox(
        connection,
        outbox_ref=fixture.flow.outbox.outbox_ref,
        transport=lambda _message: DeliveryTransportResult.published(
            "transport-receipt:42"
        ),
    )

    assert published.status == "published"
    assert connection.lifecycle.publication_state == "published"
    assert connection.lifecycle.delivery_state == "published"
    assert len(connection.tables["delivery_attempts"]) == 2
    assert len(connection.tables["customer_publications"]) == 1

    replayed = deliver_persisted_outbox(
        connection,
        outbox_ref=fixture.flow.outbox.outbox_ref,
        transport=lambda _message: pytest.fail("published outbox must not be resent"),
    )
    assert replayed.replayed is True
    assert replayed.attempt_ref == published.attempt_ref


def test_delivery_entrypoint_has_only_persisted_outbox_and_transport_boundary() -> None:
    signature = inspect.signature(deliver_persisted_outbox)
    assert tuple(signature.parameters) == ("connection", "outbox_ref", "transport")
    source = inspect.getsource(deliver_persisted_outbox).lower()
    assert not re.search(r"\b(llm|query|verifier)\b", source)
    assert "customer_payload" not in signature.parameters
