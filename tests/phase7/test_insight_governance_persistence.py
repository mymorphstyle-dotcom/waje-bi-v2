from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping

import pytest

from bi_agent.runtime.insight_governance_persistence import (
    InsightGovernancePersistenceError,
    persist_guardrail_promotion,
    persist_insight_quality_evaluation,
)
from bi_agent.runtime.insight_quality_rubric import InsightQualityRubric
from bi_agent.runtime.publication_authority import (
    GuardrailPromotionRecord,
    InsightQualityEvaluation,
    NarrativeAttemptRequest,
)
from tests.phase7.test_publication_authority import (
    QUALITY_REASONS,
    QUALITY_SCORES,
    _context,
    _quality_case_snapshot,
    _quality_model_profile,
    _review_writer_attempt,
)


class _Cursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class _Connection:
    def __init__(self, publications: tuple[tuple[str, Any], ...]) -> None:
        writer_sources = tuple(
            (owner_ref, publication, _review_writer_attempt(publication))
            for owner_ref, publication in publications
        )
        self.tables: dict[str, list[dict[str, Any]]] = {
            "publication_revisions": [
                {
                    "publication_ref": publication.publication_ref,
                    "owner_ref": owner_ref,
                    "run_attempt_id": publication.run_attempt_id,
                    "payload": publication.to_dict(),
                    "publication_digest": publication.publication_digest,
                }
                for owner_ref, publication in publications
            ],
            "narrative_writer_attempts": [
                {
                    "owner_ref": owner_ref,
                    "run_attempt_id": publication.run_attempt_id,
                    "attempt_id": writer_attempt.attempt_id,
                    "writer_attempt_ref": writer_attempt.writer_attempt_ref,
                    "provider_response_ref": writer_attempt.provider_response_ref,
                    "content_digest": writer_attempt.content_digest,
                    "payload": writer_attempt.to_dict(),
                }
                for owner_ref, publication, writer_attempt in writer_sources
            ],
            "narrative_documents": [
                {
                    "owner_ref": owner_ref,
                    "run_attempt_id": publication.run_attempt_id,
                    "narrative_id": publication.narrative_id,
                    "writer_attempt_ref": writer_attempt.writer_attempt_ref,
                    "content_digest": publication.narrative_digest,
                }
                for owner_ref, publication, writer_attempt in writer_sources
            ],
            "restricted_provider_responses": [
                {
                    "owner_ref": owner_ref,
                    "run_attempt_id": publication.run_attempt_id,
                    "provider_response_ref": writer_attempt.provider_response_ref,
                    "content_digest": writer_attempt.provider_response_digest,
                    "provider_ref": writer_attempt.provider_ref,
                    "model_ref": writer_attempt.model_ref,
                    "input_ref": writer_attempt.input_ref,
                    "input_digest": writer_attempt.input_digest,
                    "attempt_number": writer_attempt.attempt_number,
                    "purpose": "narrative_writer",
                }
                for owner_ref, publication, writer_attempt in writer_sources
            ],
        }
        self.statements: list[tuple[str, Mapping[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self._snapshot: dict[str, list[dict[str, Any]]] | None = None

    def execute(
        self,
        statement: str,
        params: Mapping[str, Any] | None = None,
    ) -> _Cursor:
        normalized = dict(params or {})
        self.statements.append((statement, normalized))
        if "pg_advisory_xact_lock" in statement:
            self._snapshot = deepcopy(self.tables)
            return _Cursor([(1,)])
        if "insight_quality_publication_source" in statement:
            row = self._review_source_row(normalized["publication_ref"])
            return _Cursor([] if row is None else [row])
        if "guardrail_promotion_evaluations_preflight" in statement:
            refs = set(normalized["evaluation_refs"])
            rows = sorted(
                (
                    row
                    for row in self.tables.get("insight_quality_evaluations", [])
                    if row["evaluation_ref"] in refs
                ),
                key=lambda row: row["evaluation_ref"],
            )
            return _Cursor(rows)
        if "insight_governance_exact_replay:" in statement:
            table = statement.split("insight_governance_exact_replay:", 1)[1].split(
                " */", 1
            )[0]
            identity_column, identity = next(iter(normalized.items()))
            row = self._by(table, **{identity_column: identity})
            if row is None:
                return _Cursor([])
            names = tuple(
                item.strip()
                for item in statement.split("SELECT", 1)[1]
                .split("FROM", 1)[0]
                .split(",")
            )
            return _Cursor([tuple(row[name] for name in names)])
        if statement.lstrip().startswith("INSERT INTO waje_runtime."):
            return self._insert(statement, normalized)
        raise AssertionError(f"unexpected SQL: {statement}")

    def _insert(self, statement: str, params: Mapping[str, Any]) -> _Cursor:
        table = statement.split("INSERT INTO waje_runtime.", 1)[1].split()[0]
        names = tuple(
            item.strip()
            for item in statement.split("(", 1)[1].split(")", 1)[0].split(",")
        )
        identity_column = statement.split("ON CONFLICT (", 1)[1].split(")", 1)[0]
        returning = statement.split("RETURNING", 1)[1].strip().split()[0]
        json_columns = {name for name in names if f"%({name})s::jsonb" in statement}
        row = {
            name: (json.loads(params[name]) if name in json_columns else params[name])
            for name in names
        }
        if self._by(table, **{identity_column: row[identity_column]}) is not None:
            return _Cursor([])
        self.tables.setdefault(table, []).append(row)
        return _Cursor([(row[returning],)])

    def _by(self, table: str, **identity: Any) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.tables.get(table, [])
                if all(row.get(key) == value for key, value in identity.items())
            ),
            None,
        )

    def _review_source_row(self, publication_ref: str) -> dict[str, Any] | None:
        publication = self._by(
            "publication_revisions",
            publication_ref=publication_ref,
        )
        if publication is None:
            return None
        narrative = self._by(
            "narrative_documents",
            owner_ref=publication["owner_ref"],
            run_attempt_id=publication["run_attempt_id"],
            narrative_id=publication["payload"]["narrative_id"],
        )
        writer_attempt = self._by(
            "narrative_writer_attempts",
            owner_ref=publication["owner_ref"],
            run_attempt_id=publication["run_attempt_id"],
            attempt_id=publication["payload"]["narrative_attempt_id"],
            writer_attempt_ref=(
                None if narrative is None else narrative["writer_attempt_ref"]
            ),
        )
        provider_response = (
            None
            if writer_attempt is None
            else self._by(
                "restricted_provider_responses",
                owner_ref=publication["owner_ref"],
                run_attempt_id=publication["run_attempt_id"],
                provider_response_ref=writer_attempt["provider_response_ref"],
            )
        )
        return {
            "owner_ref": publication["owner_ref"],
            "run_attempt_id": publication["run_attempt_id"],
            "publication_payload": publication["payload"],
            "publication_digest": publication["publication_digest"],
            "narrative_writer_attempt_ref": (
                None if narrative is None else narrative["writer_attempt_ref"]
            ),
            "narrative_digest": (
                None if narrative is None else narrative["content_digest"]
            ),
            "writer_attempt_payload": (
                None if writer_attempt is None else writer_attempt["payload"]
            ),
            "writer_attempt_ref": (
                None if writer_attempt is None else writer_attempt["writer_attempt_ref"]
            ),
            "writer_attempt_digest": (
                None if writer_attempt is None else writer_attempt["content_digest"]
            ),
            "provider_response_ref": (
                None
                if provider_response is None
                else provider_response["provider_response_ref"]
            ),
            "provider_response_digest": (
                None
                if provider_response is None
                else provider_response["content_digest"]
            ),
            "provider_ref": (
                None if provider_response is None else provider_response["provider_ref"]
            ),
            "model_ref": (
                None if provider_response is None else provider_response["model_ref"]
            ),
            "input_ref": (
                None if provider_response is None else provider_response["input_ref"]
            ),
            "input_digest": (
                None if provider_response is None else provider_response["input_digest"]
            ),
            "attempt_number": (
                None
                if provider_response is None
                else provider_response["attempt_number"]
            ),
            "purpose": (
                None if provider_response is None else provider_response["purpose"]
            ),
        }

    def commit(self) -> None:
        self.commits += 1
        self._snapshot = None

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._snapshot is not None:
            self.tables = self._snapshot
        self._snapshot = None


def _evaluation(
    publication: Any,
    *,
    case_ref: str,
    reviewer_ref: str,
    request: NarrativeAttemptRequest | None = None,
) -> InsightQualityEvaluation:
    return InsightQualityEvaluation.review(
        publication=publication,
        rubric=InsightQualityRubric.v1(),
        evaluation_case=_quality_case_snapshot(publication, case_id=case_ref),
        model_profile=_quality_model_profile(publication),
        reviewer_ref=reviewer_ref,
        scores=QUALITY_SCORES,
        human_reasons=QUALITY_REASONS,
        narrative_attempt_request=request,
        reviewed_at="2026-07-18T14:00:00Z",
    )


def test_quality_evaluation_and_independent_attempt_request_persist_atomically() -> (
    None
):
    publication = _context().publication
    owner_ref = "owner:phase6-tests"
    connection = _Connection(((owner_ref, publication),))
    request = NarrativeAttemptRequest.create(
        publication=publication,
        requested_attempt_id="writer-attempt:independent-quality-review",
        reason_dimensions=("novelty", "competing_hypotheses"),
        requested_by="reviewer:business-42",
    )
    evaluation = _evaluation(
        publication,
        case_ref="eval-case:paid-amount-quality",
        reviewer_ref="reviewer:business-42",
        request=request,
    )

    inserted = persist_insight_quality_evaluation(
        connection,
        owner_ref=owner_ref,
        publication=publication,
        evaluation=evaluation,
        narrative_attempt_request=request,
    )
    replayed = persist_insight_quality_evaluation(
        connection,
        owner_ref=owner_ref,
        publication=publication,
        evaluation=evaluation,
        narrative_attempt_request=request,
    )

    assert inserted.status == "inserted"
    assert replayed.status == "replayed"
    assert len(connection.tables["narrative_attempt_requests"]) == 1
    assert len(connection.tables["insight_quality_evaluations"]) == 1
    persisted = connection.tables["insight_quality_evaluations"][0]
    assert persisted["run_attempt_id"] == publication.run_attempt_id
    assert persisted["advisory"] is True
    assert persisted["result"] == "request_independent_narrative_attempt"
    assert persisted["rubric_ref"] == evaluation.rubric_ref
    assert persisted["rubric_digest"] == evaluation.rubric_digest
    assert persisted["evaluation_case_ref"] == evaluation.evaluation_case_ref
    assert persisted["evaluation_case_digest"] == evaluation.evaluation_case_digest
    assert persisted["model_profile_ref"] == evaluation.model_profile_ref
    assert persisted["model_profile_digest"] == evaluation.model_profile_digest
    assert persisted["human_reasons"] == QUALITY_REASONS


def test_quality_evaluation_rejects_persisted_publication_drift() -> None:
    publication = _context().publication
    owner_ref = "owner:phase6-tests"
    connection = _Connection(((owner_ref, publication),))
    connection.tables["publication_revisions"][0]["payload"]["projection_id"] = (
        "projection:drifted"
    )
    evaluation = _evaluation(
        publication,
        case_ref="eval-case:drift",
        reviewer_ref="reviewer:business-42",
    )

    with pytest.raises(
        InsightGovernancePersistenceError,
        match="insight_quality_source_publication_conflict",
    ):
        persist_insight_quality_evaluation(
            connection,
            owner_ref=owner_ref,
            publication=publication,
            evaluation=evaluation,
            narrative_attempt_request=None,
        )

    assert "insight_quality_evaluations" not in connection.tables
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_quality_evaluation_rejects_persisted_writer_authority_drift() -> None:
    publication = _context().publication
    owner_ref = "owner:phase6-tests"
    connection = _Connection(((owner_ref, publication),))
    connection.tables["restricted_provider_responses"][0]["model_ref"] = "model:drifted"
    evaluation = _evaluation(
        publication,
        case_ref="eval-case:writer-drift",
        reviewer_ref="reviewer:business-42",
    )

    with pytest.raises(
        InsightGovernancePersistenceError,
        match="insight_quality_writer_authority_conflict",
    ):
        persist_insight_quality_evaluation(
            connection,
            owner_ref=owner_ref,
            publication=publication,
            evaluation=evaluation,
            narrative_attempt_request=None,
        )

    assert "insight_quality_evaluations" not in connection.tables
    assert connection.rollbacks == 1


def test_guardrail_promotion_closes_persisted_evaluations_across_runs() -> None:
    owner_ref = "owner:phase6-tests"
    first_publication = _context(run_attempt_id="run-attempt:quality-1").publication
    second_publication = _context(run_attempt_id="run-attempt:quality-2").publication
    connection = _Connection(
        (
            (owner_ref, first_publication),
            (owner_ref, second_publication),
        )
    )
    first = _evaluation(
        first_publication,
        case_ref="eval-case:quality-1",
        reviewer_ref="reviewer:business-1",
    )
    second = _evaluation(
        second_publication,
        case_ref="eval-case:quality-2",
        reviewer_ref="reviewer:business-2",
    )
    for publication, evaluation in (
        (first_publication, first),
        (second_publication, second),
    ):
        persist_insight_quality_evaluation(
            connection,
            owner_ref=owner_ref,
            publication=publication,
            evaluation=evaluation,
            narrative_attempt_request=None,
        )
    scope = "governance:waje-runtime-guardrails"
    promotion = GuardrailPromotionRecord.approve(
        governance_scope_ref=scope,
        evaluations=(first, second),
        generalizable_pattern_ref="failure-pattern:missing-alternatives",
        recurrence_evidence_refs=("eval-run:1", "eval-run:2"),
        human_validation_ref="validation:pattern-review-1",
        business_owner_ref="owner:business-insight-quality",
        system_owner_ref="owner:agent-runtime",
        runtime_guardrail_ref="guardrail:competing-hypothesis-coverage",
        approved_at="2026-07-18T15:00:00Z",
    )

    inserted = persist_guardrail_promotion(
        connection,
        governance_scope_ref=scope,
        promotion=promotion,
        evaluations=(first, second),
    )
    replayed = persist_guardrail_promotion(
        connection,
        governance_scope_ref=scope,
        promotion=promotion,
        evaluations=(first, second),
    )

    assert inserted.status == "inserted"
    assert replayed.status == "replayed"
    run_ids = {
        row["run_attempt_id"]
        for row in connection.tables["insight_quality_evaluations"]
    }
    assert run_ids == {"run-attempt:quality-1", "run-attempt:quality-2"}
    persisted = connection.tables["guardrail_promotion_records"][0]
    assert persisted["governance_scope_ref"] == scope
    assert "run_attempt_id" not in persisted
    assert "owner_ref" not in persisted


def test_guardrail_promotion_requires_every_evaluation_to_be_persisted() -> None:
    owner_ref = "owner:phase6-tests"
    first_publication = _context(run_attempt_id="run-attempt:quality-1").publication
    second_publication = _context(run_attempt_id="run-attempt:quality-2").publication
    connection = _Connection(
        (
            (owner_ref, first_publication),
            (owner_ref, second_publication),
        )
    )
    first = _evaluation(
        first_publication,
        case_ref="eval-case:quality-1",
        reviewer_ref="reviewer:business-1",
    )
    second = _evaluation(
        second_publication,
        case_ref="eval-case:quality-2",
        reviewer_ref="reviewer:business-2",
    )
    persist_insight_quality_evaluation(
        connection,
        owner_ref=owner_ref,
        publication=first_publication,
        evaluation=first,
        narrative_attempt_request=None,
    )
    scope = "governance:waje-runtime-guardrails"
    promotion = GuardrailPromotionRecord.approve(
        governance_scope_ref=scope,
        evaluations=(first, second),
        generalizable_pattern_ref="failure-pattern:missing-alternatives",
        recurrence_evidence_refs=("eval-run:1", "eval-run:2"),
        human_validation_ref="validation:pattern-review-1",
        business_owner_ref="owner:business-insight-quality",
        system_owner_ref="owner:agent-runtime",
        runtime_guardrail_ref="guardrail:competing-hypothesis-coverage",
        approved_at="2026-07-18T15:00:00Z",
    )

    with pytest.raises(
        InsightGovernancePersistenceError,
        match="guardrail_promotion_evaluation_missing",
    ):
        persist_guardrail_promotion(
            connection,
            governance_scope_ref=scope,
            promotion=promotion,
            evaluations=(first, second),
        )

    assert "guardrail_promotion_records" not in connection.tables
    assert connection.rollbacks == 1
