from __future__ import annotations

from dataclasses import replace

import pytest

from bi_agent.runtime.insight_quality_rubric import (
    INSIGHT_QUALITY_DIMENSIONS,
    InsightEvaluationCaseSnapshot,
    InsightQualityRubric,
    InsightQualityRubricContractError,
)
from bi_agent.runtime.publication_authority import (
    InsightQualityEvaluation,
    PublicationAuthorityContractError,
)
from tests.phase7.test_publication_authority import (
    QUALITY_REASONS,
    _context,
    _quality_case_snapshot,
    _quality_model_profile,
)


def test_v1_rubric_has_six_dimensions_with_anchored_interpolation() -> None:
    rubric = InsightQualityRubric.v1()

    assert rubric.rubric_version == "insight-quality-rubric.v1"
    assert tuple(item.dimension for item in rubric.dimensions) == (
        INSIGHT_QUALITY_DIMENSIONS
    )
    for dimension in rubric.dimensions:
        assert dimension.score_1_anchor
        assert dimension.score_3_anchor
        assert dimension.score_5_anchor
        assert "Use 2" in dimension.score_2_interpolation
        assert "Use 4" in dimension.score_4_interpolation
    assert InsightQualityRubric.from_dict(rubric.to_dict()) == rubric


def test_v1_rubric_rejects_content_drift_under_the_same_version() -> None:
    payload = InsightQualityRubric.v1().to_dict()
    payload["dimensions"][0]["score_5_anchor"] = "Changed anchor."

    with pytest.raises(
        InsightQualityRubricContractError,
        match="insight_quality_rubric_integrity_invalid",
    ):
        InsightQualityRubric.from_dict(payload)


def test_additional_case_can_remain_unclassified_in_advisory_review() -> None:
    publication = _context().publication
    snapshot = InsightEvaluationCaseSnapshot.create(
        acceptance_summary_version="phase7-customer-publication-acceptance-summary.v2",
        acceptance_source="persisted_customer_publication",
        acceptance_summary_digest="1" * 64,
        acceptance_status="passed",
        case_id="additional-case",
        question_family=None,
        variant="additional",
        user_message="一个额外的自然语言验收问题",
        review_focus="记录回答质量，不改变交付。",
        run_attempt_id=publication.run_attempt_id,
        publication_ref=publication.publication_ref,
        publication_digest=publication.publication_digest,
        customer_payload_ref="customer-payload:additional-case",
        customer_payload_digest="2" * 64,
    )

    assert snapshot.question_family is None
    assert InsightEvaluationCaseSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_paired_case_requires_a_question_family() -> None:
    publication = _context().publication

    with pytest.raises(
        InsightQualityRubricContractError,
        match="insight_evaluation_case_question_family_invalid",
    ):
        InsightEvaluationCaseSnapshot.create(
            acceptance_summary_version="phase7-customer-publication-acceptance-summary.v2",
            acceptance_source="persisted_customer_publication",
            acceptance_summary_digest="1" * 64,
            acceptance_status="passed",
            case_id="paired-case",
            question_family=None,
            variant="original",
            user_message="一个成对验收问题",
            review_focus="验证同一问题家族。",
            run_attempt_id=publication.run_attempt_id,
            publication_ref=publication.publication_ref,
            publication_digest=publication.publication_digest,
            customer_payload_ref="customer-payload:paired-case",
            customer_payload_digest="2" * 64,
        )


def test_scores_remain_human_advisory_without_an_automatic_threshold() -> None:
    publication = _context().publication
    evaluation = InsightQualityEvaluation.review(
        publication=publication,
        rubric=InsightQualityRubric.v1(),
        evaluation_case=_quality_case_snapshot(
            publication,
            case_id="eval-case:all-low-scores",
        ),
        model_profile=_quality_model_profile(publication),
        reviewer_ref="reviewer:business-42",
        scores={dimension: 1 for dimension in INSIGHT_QUALITY_DIMENSIONS},
        human_reasons=QUALITY_REASONS,
        narrative_attempt_request=None,
        reviewed_at="2026-07-18T16:00:00Z",
    )

    assert evaluation.result == "retain_publication"
    assert evaluation.narrative_attempt_request_ref is None
    assert evaluation.advisory is True


def test_evaluation_requires_six_non_empty_human_reasons() -> None:
    publication = _context().publication
    incomplete_reasons = dict(QUALITY_REASONS)
    incomplete_reasons.pop("novelty")

    with pytest.raises(
        PublicationAuthorityContractError,
        match="insight_quality_review_context_invalid",
    ):
        InsightQualityEvaluation.review(
            publication=publication,
            rubric=InsightQualityRubric.v1(),
            evaluation_case=_quality_case_snapshot(
                publication,
                case_id="eval-case:missing-human-reason",
            ),
            model_profile=_quality_model_profile(publication),
            reviewer_ref="reviewer:business-42",
            scores={dimension: 3 for dimension in INSIGHT_QUALITY_DIMENSIONS},
            human_reasons=incomplete_reasons,
            narrative_attempt_request=None,
            reviewed_at="2026-07-18T16:00:00Z",
        )


def test_evaluation_rejects_case_snapshot_publication_drift() -> None:
    publication = _context().publication
    case = _quality_case_snapshot(
        publication,
        case_id="eval-case:publication-drift",
    )

    with pytest.raises(
        PublicationAuthorityContractError,
        match="insight_quality_review_context_invalid",
    ):
        InsightQualityEvaluation.review(
            publication=publication,
            rubric=InsightQualityRubric.v1(),
            evaluation_case=replace(
                case,
                publication_digest="0" * 64,
            ),
            model_profile=_quality_model_profile(publication),
            reviewer_ref="reviewer:business-42",
            scores={dimension: 3 for dimension in INSIGHT_QUALITY_DIMENSIONS},
            human_reasons=QUALITY_REASONS,
            narrative_attempt_request=None,
            reviewed_at="2026-07-18T16:00:00Z",
        )
