from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.insight_quality_rubric import (
    InsightModelProfileSnapshot,
    InsightQualityRubricContractError,
)
from bi_agent.runtime.narrative_authority import NarrativeWriterAttempt
from bi_agent.runtime.publication_authority import (
    GuardrailPromotionRecord,
    InsightQualityEvaluation,
    NarrativeAttemptRequest,
    PublicationRevision,
)


class InsightGovernancePersistenceError(ValueError):
    pass


@dataclass(frozen=True)
class InsightQualityPersistenceResult:
    evaluation_ref: str
    narrative_attempt_request_ref: str | None
    run_attempt_id: str
    status: str


@dataclass(frozen=True)
class GuardrailPromotionPersistenceResult:
    promotion_ref: str
    governance_scope_ref: str
    status: str


@dataclass(frozen=True)
class InsightReviewPublication:
    owner_ref: str
    publication: PublicationRevision
    model_profile: InsightModelProfileSnapshot


@dataclass(frozen=True)
class _InsertRecord:
    table: str
    identity_column: str
    columns: Mapping[str, Any]
    json_columns: frozenset[str] = frozenset({"payload"})


_PUBLICATION_SOURCE_SQL = """
/* insight_quality_publication_source */
SELECT
  publication.owner_ref,
  publication.run_attempt_id,
  publication.payload AS publication_payload,
  publication.publication_digest,
  narrative.writer_attempt_ref AS narrative_writer_attempt_ref,
  narrative.content_digest AS narrative_digest,
  writer_attempt.payload AS writer_attempt_payload,
  writer_attempt.writer_attempt_ref,
  writer_attempt.content_digest AS writer_attempt_digest,
  provider_response.provider_response_ref,
  provider_response.content_digest AS provider_response_digest,
  provider_response.provider_ref,
  provider_response.model_ref,
  provider_response.input_ref,
  provider_response.input_digest,
  provider_response.attempt_number,
  provider_response.purpose
FROM waje_runtime.publication_revisions publication
LEFT JOIN waje_runtime.narrative_documents narrative
  ON narrative.owner_ref = publication.owner_ref
 AND narrative.run_attempt_id = publication.run_attempt_id
 AND narrative.narrative_id = publication.narrative_id
LEFT JOIN waje_runtime.narrative_writer_attempts writer_attempt
  ON writer_attempt.owner_ref = narrative.owner_ref
 AND writer_attempt.run_attempt_id = narrative.run_attempt_id
 AND writer_attempt.writer_attempt_ref = narrative.writer_attempt_ref
 AND writer_attempt.attempt_id = publication.narrative_attempt_id
LEFT JOIN waje_runtime.restricted_provider_responses provider_response
  ON provider_response.owner_ref = writer_attempt.owner_ref
 AND provider_response.run_attempt_id = writer_attempt.run_attempt_id
 AND provider_response.provider_response_ref = writer_attempt.provider_response_ref
WHERE publication.publication_ref = %(publication_ref)s
FOR SHARE OF publication
"""


_PROMOTION_EVALUATIONS_SQL = """
/* guardrail_promotion_evaluations_preflight */
SELECT evaluation_ref, owner_ref, run_attempt_id, payload, content_digest
FROM waje_runtime.insight_quality_evaluations
WHERE evaluation_ref = ANY(%(evaluation_refs)s)
ORDER BY evaluation_ref
FOR SHARE
"""


def load_insight_review_publication(
    connection: Any,
    *,
    publication_ref: str,
) -> InsightReviewPublication:
    requested_ref = _required_string(
        publication_ref,
        "insight_quality_publication_ref_invalid",
    )
    row = connection.execute(
        _PUBLICATION_SOURCE_SQL,
        {"publication_ref": requested_ref},
    ).fetchone()
    if row is None:
        raise InsightGovernancePersistenceError(
            "insight_quality_source_publication_missing"
        )
    return _review_source_from_row(row, requested_ref=requested_ref)


def persist_insight_quality_evaluation(
    connection: Any,
    *,
    owner_ref: str,
    publication: PublicationRevision,
    evaluation: InsightQualityEvaluation,
    narrative_attempt_request: NarrativeAttemptRequest | None,
) -> InsightQualityPersistenceResult:
    owner = _required_string(owner_ref, "insight_quality_owner_ref_invalid")
    publication = _validated_publication(publication)
    request = _validated_request(
        narrative_attempt_request,
        publication=publication,
    )
    evaluated = _validated_evaluation(
        evaluation,
        publication=publication,
        narrative_attempt_request=request,
    )
    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"insight-quality:{publication.publication_ref}"},
        )
        row = connection.execute(
            _PUBLICATION_SOURCE_SQL,
            {"publication_ref": publication.publication_ref},
        ).fetchone()
        if row is None:
            raise InsightGovernancePersistenceError(
                "insight_quality_source_publication_missing"
            )
        stored_source = _review_source_from_row(
            row,
            requested_ref=publication.publication_ref,
        )
        if (
            stored_source.owner_ref != owner
            or stored_source.publication != publication
            or stored_source.model_profile != evaluated.model_profile
        ):
            raise InsightGovernancePersistenceError(
                "insight_quality_source_publication_conflict"
            )

        inserted = False
        if request is not None:
            inserted = _insert_exact(
                connection,
                _request_record(
                    owner_ref=owner, publication=publication, request=request
                ),
            )
        inserted = (
            _insert_exact(
                connection,
                _evaluation_record(
                    owner_ref=owner,
                    publication=publication,
                    evaluation=evaluated,
                ),
            )
            or inserted
        )
        connection.commit()
        return InsightQualityPersistenceResult(
            evaluation_ref=evaluated.evaluation_ref,
            narrative_attempt_request_ref=(
                None if request is None else request.request_ref
            ),
            run_attempt_id=publication.run_attempt_id,
            status="inserted" if inserted else "replayed",
        )
    except Exception:
        connection.rollback()
        raise


def persist_guardrail_promotion(
    connection: Any,
    *,
    governance_scope_ref: str,
    promotion: GuardrailPromotionRecord,
    evaluations: Sequence[InsightQualityEvaluation],
) -> GuardrailPromotionPersistenceResult:
    scope = _required_string(
        governance_scope_ref,
        "guardrail_promotion_governance_scope_invalid",
    )
    normalized_evaluations = tuple(evaluations)
    try:
        validated = GuardrailPromotionRecord.from_dict(
            promotion.to_dict(),
            evaluations=normalized_evaluations,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise InsightGovernancePersistenceError(
            "guardrail_promotion_record_invalid"
        ) from exc
    if validated != promotion or validated.governance_scope_ref != scope:
        raise InsightGovernancePersistenceError("guardrail_promotion_scope_conflict")

    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"guardrail-governance:{scope}"},
        )
        rows = connection.execute(
            _PROMOTION_EVALUATIONS_SQL,
            {"evaluation_refs": list(validated.evaluation_refs)},
        ).fetchall()
        stored = {str(_field(row, "evaluation_ref", 0)): row for row in rows}
        expected = {
            evaluation.evaluation_ref: evaluation
            for evaluation in normalized_evaluations
        }
        if set(stored) != set(expected):
            raise InsightGovernancePersistenceError(
                "guardrail_promotion_evaluation_missing"
            )
        for ref, evaluation in expected.items():
            row = stored[ref]
            if (
                canonical_value(_payload(_field(row, "payload", 3)))
                != canonical_value(evaluation.to_dict())
                or str(_field(row, "content_digest", 4) or "")
                != evaluation.content_digest
            ):
                raise InsightGovernancePersistenceError(
                    "guardrail_promotion_evaluation_conflict"
                )

        inserted = _insert_exact(
            connection,
            _promotion_record(promotion=validated),
        )
        connection.commit()
        return GuardrailPromotionPersistenceResult(
            promotion_ref=validated.promotion_ref,
            governance_scope_ref=scope,
            status="inserted" if inserted else "replayed",
        )
    except Exception:
        connection.rollback()
        raise


def _validated_publication(publication: PublicationRevision) -> PublicationRevision:
    if type(publication) is not PublicationRevision:
        raise InsightGovernancePersistenceError("insight_quality_publication_invalid")
    payload = publication.to_dict()
    if set(payload) != set(PublicationRevision.__dataclass_fields__):
        raise InsightGovernancePersistenceError("insight_quality_publication_invalid")
    digest = canonical_digest(
        {
            key: value
            for key, value in payload.items()
            if key not in {"publication_ref", "publication_digest", "published_at"}
        }
    )
    if (
        publication.publication_digest != digest
        or publication.publication_ref != "publication-revision:sha256:" + digest
    ):
        raise InsightGovernancePersistenceError("insight_quality_publication_invalid")
    return publication


def _review_source_from_row(
    row: Any,
    *,
    requested_ref: str,
) -> InsightReviewPublication:
    owner_ref = _required_string(
        _field(row, "owner_ref", 0),
        "insight_quality_source_publication_conflict",
    )
    raw_publication = _payload(_field(row, "publication_payload", 2))
    if not isinstance(raw_publication, Mapping) or set(raw_publication) != set(
        PublicationRevision.__dataclass_fields__
    ):
        raise InsightGovernancePersistenceError(
            "insight_quality_source_publication_conflict"
        )
    try:
        publication = _validated_publication(
            PublicationRevision(**canonical_value(raw_publication))
        )
    except (TypeError, ValueError) as exc:
        raise InsightGovernancePersistenceError(
            "insight_quality_source_publication_conflict"
        ) from exc
    if (
        publication.publication_ref != requested_ref
        or publication.run_attempt_id != str(_field(row, "run_attempt_id", 1) or "")
        or publication.publication_digest
        != str(_field(row, "publication_digest", 3) or "")
    ):
        raise InsightGovernancePersistenceError(
            "insight_quality_source_publication_conflict"
        )

    raw_writer_attempt = _payload(_field(row, "writer_attempt_payload", 6))
    if raw_writer_attempt is None:
        raise InsightGovernancePersistenceError(
            "insight_quality_writer_authority_missing"
        )
    try:
        writer_attempt = NarrativeWriterAttempt.from_dict(raw_writer_attempt)
    except (TypeError, ValueError) as exc:
        raise InsightGovernancePersistenceError(
            "insight_quality_writer_authority_conflict"
        ) from exc
    response = writer_attempt.provider_response
    if (
        writer_attempt.attempt_id != publication.narrative_attempt_id
        or writer_attempt.writer_attempt_ref
        != str(_field(row, "narrative_writer_attempt_ref", 4) or "")
        or publication.narrative_digest != str(_field(row, "narrative_digest", 5) or "")
        or writer_attempt.writer_attempt_ref
        != str(_field(row, "writer_attempt_ref", 7) or "")
        or writer_attempt.content_digest
        != str(_field(row, "writer_attempt_digest", 8) or "")
        or response.response_ref != str(_field(row, "provider_response_ref", 9) or "")
        or response.content_digest
        != str(_field(row, "provider_response_digest", 10) or "")
        or response.provider_ref != str(_field(row, "provider_ref", 11) or "")
        or response.model_ref != str(_field(row, "model_ref", 12) or "")
        or response.input_ref != str(_field(row, "input_ref", 13) or "")
        or response.input_digest != str(_field(row, "input_digest", 14) or "")
        or response.attempt_number != _field(row, "attempt_number", 15)
        or _field(row, "purpose", 16) != "narrative_writer"
    ):
        raise InsightGovernancePersistenceError(
            "insight_quality_writer_authority_conflict"
        )
    try:
        model_profile = InsightModelProfileSnapshot.create(
            source_publication_ref=publication.publication_ref,
            source_publication_digest=publication.publication_digest,
            source_narrative_id=publication.narrative_id,
            source_narrative_attempt_id=publication.narrative_attempt_id,
            writer_attempt_ref=writer_attempt.writer_attempt_ref,
            writer_attempt_digest=writer_attempt.content_digest,
            writer_input_ref=writer_attempt.input_ref,
            writer_input_digest=writer_attempt.input_digest,
            writer_attempt_number=writer_attempt.attempt_number,
            provider_ref=writer_attempt.provider_ref,
            model_ref=writer_attempt.model_ref,
            provider_response_ref=writer_attempt.provider_response_ref,
            provider_response_digest=writer_attempt.provider_response_digest,
        )
    except InsightQualityRubricContractError as exc:
        raise InsightGovernancePersistenceError(
            "insight_quality_writer_authority_conflict"
        ) from exc
    return InsightReviewPublication(
        owner_ref=owner_ref,
        publication=publication,
        model_profile=model_profile,
    )


def _validated_request(
    request: NarrativeAttemptRequest | None,
    *,
    publication: PublicationRevision,
) -> NarrativeAttemptRequest | None:
    if request is None:
        return None
    try:
        replayed = NarrativeAttemptRequest.from_dict(
            request.to_dict(),
            publication=publication,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise InsightGovernancePersistenceError(
            "narrative_attempt_request_invalid"
        ) from exc
    if replayed != request:
        raise InsightGovernancePersistenceError("narrative_attempt_request_invalid")
    return replayed


def _validated_evaluation(
    evaluation: InsightQualityEvaluation,
    *,
    publication: PublicationRevision,
    narrative_attempt_request: NarrativeAttemptRequest | None,
) -> InsightQualityEvaluation:
    try:
        replayed = InsightQualityEvaluation.from_dict(
            evaluation.to_dict(),
            publication=publication,
            narrative_attempt_request=narrative_attempt_request,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise InsightGovernancePersistenceError(
            "insight_quality_evaluation_invalid"
        ) from exc
    if replayed != evaluation:
        raise InsightGovernancePersistenceError("insight_quality_evaluation_invalid")
    return replayed


def _request_record(
    *,
    owner_ref: str,
    publication: PublicationRevision,
    request: NarrativeAttemptRequest,
) -> _InsertRecord:
    payload = request.to_dict()
    return _InsertRecord(
        table="narrative_attempt_requests",
        identity_column="request_ref",
        columns={
            **payload,
            "owner_ref": owner_ref,
            "run_attempt_id": publication.run_attempt_id,
            "payload": payload,
        },
        json_columns=frozenset({"reason_dimensions", "payload"}),
    )


def _evaluation_record(
    *,
    owner_ref: str,
    publication: PublicationRevision,
    evaluation: InsightQualityEvaluation,
) -> _InsertRecord:
    payload = evaluation.to_dict()
    return _InsertRecord(
        table="insight_quality_evaluations",
        identity_column="evaluation_ref",
        columns={
            **payload,
            "owner_ref": owner_ref,
            "run_attempt_id": publication.run_attempt_id,
            "payload": payload,
        },
        json_columns=frozenset(
            {
                "rubric",
                "evaluation_case",
                "model_profile",
                "scores",
                "human_reasons",
                "payload",
            }
        ),
    )


def _promotion_record(*, promotion: GuardrailPromotionRecord) -> _InsertRecord:
    payload = promotion.to_dict()
    return _InsertRecord(
        table="guardrail_promotion_records",
        identity_column="promotion_ref",
        columns={**payload, "payload": payload},
        json_columns=frozenset(
            {"evaluation_refs", "case_refs", "recurrence_evidence_refs", "payload"}
        ),
    )


def _insert_exact(connection: Any, record: _InsertRecord) -> bool:
    names = tuple(record.columns)
    values = tuple(
        f"%({name})s::jsonb" if name in record.json_columns else f"%({name})s"
        for name in names
    )
    params = {
        name: (
            json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"))
            if name in record.json_columns
            else value
        )
        for name, value in record.columns.items()
    }
    inserted = connection.execute(
        f"""
        INSERT INTO waje_runtime.{record.table} ({", ".join(names)})
        VALUES ({", ".join(values)})
        ON CONFLICT ({record.identity_column}) DO NOTHING
        RETURNING {record.identity_column}
        """,
        params,
    ).fetchone()
    if inserted is not None:
        if str(_field(inserted, record.identity_column, 0)) != str(
            record.columns[record.identity_column]
        ):
            raise InsightGovernancePersistenceError(
                f"insight_governance_insert_identity_conflict:{record.table}"
            )
        return True

    row = connection.execute(
        f"""
        /* insight_governance_exact_replay:{record.table} */
        SELECT {", ".join(names)}
        FROM waje_runtime.{record.table}
        WHERE {record.identity_column} = %({record.identity_column})s
        """,
        {record.identity_column: record.columns[record.identity_column]},
    ).fetchone()
    if row is None:
        raise InsightGovernancePersistenceError(
            f"insight_governance_exact_replay_missing:{record.table}"
        )
    for index, name in enumerate(names):
        stored = _field(row, name, index)
        if name in record.json_columns:
            stored = _payload(stored)
        if canonical_value(stored) != canonical_value(record.columns[name]):
            raise InsightGovernancePersistenceError(
                f"insight_governance_exact_replay_conflict:{record.table}:{name}"
            )
    return False


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InsightGovernancePersistenceError(error)
    return value


def _payload(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise InsightGovernancePersistenceError(
                "insight_governance_json_invalid"
            ) from exc
    return value


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    if hasattr(row, name):
        return getattr(row, name)
    return row[index]
