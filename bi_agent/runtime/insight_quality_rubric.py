from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


class InsightQualityRubricContractError(ValueError):
    pass


INSIGHT_QUALITY_DIMENSIONS = (
    "explanation_value",
    "novelty",
    "decision_usefulness",
    "competing_hypotheses",
    "uncertainty_handling",
    "actionability",
)
INSIGHT_REVIEW_ACCEPTANCE_SUMMARY_VERSION = (
    "phase7-customer-publication-acceptance-summary.v2"
)
INSIGHT_REVIEW_ACCEPTANCE_SOURCE = "persisted_customer_publication"


@dataclass(frozen=True)
class InsightQualityDimensionRubric:
    dimension: str
    label: str
    score_1_anchor: str
    score_3_anchor: str
    score_5_anchor: str
    score_2_interpolation: str
    score_4_interpolation: str

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "InsightQualityDimensionRubric":
        payload = _strict_shape(
            payload,
            cls,
            "insight_quality_dimension_rubric_shape_invalid",
        )
        return cls(
            dimension=_required_string(
                payload["dimension"],
                "insight_quality_dimension_rubric_invalid",
            ),
            label=_required_string(
                payload["label"],
                "insight_quality_dimension_rubric_invalid",
            ),
            score_1_anchor=_required_string(
                payload["score_1_anchor"],
                "insight_quality_dimension_rubric_invalid",
            ),
            score_3_anchor=_required_string(
                payload["score_3_anchor"],
                "insight_quality_dimension_rubric_invalid",
            ),
            score_5_anchor=_required_string(
                payload["score_5_anchor"],
                "insight_quality_dimension_rubric_invalid",
            ),
            score_2_interpolation=_required_string(
                payload["score_2_interpolation"],
                "insight_quality_dimension_rubric_invalid",
            ),
            score_4_interpolation=_required_string(
                payload["score_4_interpolation"],
                "insight_quality_dimension_rubric_invalid",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class InsightQualityRubric:
    rubric_ref: str
    rubric_version: str
    dimensions: tuple[InsightQualityDimensionRubric, ...]
    rubric_digest: str

    @classmethod
    def v1(cls) -> "InsightQualityRubric":
        dimensions = tuple(
            InsightQualityDimensionRubric(**payload)
            for payload in _V1_DIMENSION_RUBRICS
        )
        body = {
            "rubric_version": "insight-quality-rubric.v1",
            "dimensions": dimensions,
        }
        digest = canonical_digest(body)
        return cls(
            rubric_ref="insight-quality-rubric:v1:sha256:" + digest,
            rubric_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InsightQualityRubric":
        payload = _strict_shape(
            payload,
            cls,
            "insight_quality_rubric_shape_invalid",
        )
        raw_dimensions = payload["dimensions"]
        if isinstance(raw_dimensions, (str, bytes)) or not isinstance(
            raw_dimensions, Sequence
        ):
            raise InsightQualityRubricContractError(
                "insight_quality_rubric_dimensions_invalid"
            )
        dimensions = tuple(
            InsightQualityDimensionRubric.from_dict(item) for item in raw_dimensions
        )
        rebuilt = cls.v1()
        if (
            tuple(item.dimension for item in dimensions) != INSIGHT_QUALITY_DIMENSIONS
            or canonical_value(payload) != rebuilt.to_dict()
        ):
            raise InsightQualityRubricContractError(
                "insight_quality_rubric_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class InsightEvaluationCaseSnapshot:
    case_snapshot_ref: str
    acceptance_summary_version: str
    acceptance_source: str
    acceptance_summary_digest: str
    acceptance_status: str
    case_id: str
    question_family: str | None
    variant: str
    user_message: str
    review_focus: str
    run_attempt_id: str
    publication_ref: str
    publication_digest: str
    customer_payload_ref: str
    customer_payload_digest: str
    case_snapshot_digest: str

    @classmethod
    def create(
        cls,
        *,
        acceptance_summary_version: str,
        acceptance_source: str,
        acceptance_summary_digest: str,
        acceptance_status: str,
        case_id: str,
        question_family: str | None,
        variant: str,
        user_message: str,
        review_focus: str,
        run_attempt_id: str,
        publication_ref: str,
        publication_digest: str,
        customer_payload_ref: str,
        customer_payload_digest: str,
    ) -> "InsightEvaluationCaseSnapshot":
        if acceptance_status != "passed":
            raise InsightQualityRubricContractError(
                "insight_evaluation_case_acceptance_status_invalid"
            )
        if acceptance_summary_version != INSIGHT_REVIEW_ACCEPTANCE_SUMMARY_VERSION:
            raise InsightQualityRubricContractError(
                "insight_evaluation_case_summary_version_invalid"
            )
        if acceptance_source != INSIGHT_REVIEW_ACCEPTANCE_SOURCE:
            raise InsightQualityRubricContractError(
                "insight_evaluation_case_acceptance_source_invalid"
            )
        if variant not in {"original", "paraphrase", "additional"}:
            raise InsightQualityRubricContractError(
                "insight_evaluation_case_variant_invalid"
            )
        if question_family is None:
            if variant != "additional":
                raise InsightQualityRubricContractError(
                    "insight_evaluation_case_question_family_invalid"
                )
            normalized_question_family = None
        else:
            normalized_question_family = _required_string(
                question_family,
                "insight_evaluation_case_question_family_invalid",
            )
        body = {
            "acceptance_summary_version": _required_string(
                acceptance_summary_version,
                "insight_evaluation_case_summary_version_invalid",
            ),
            "acceptance_source": _required_string(
                acceptance_source,
                "insight_evaluation_case_acceptance_source_invalid",
            ),
            "acceptance_summary_digest": _digest_string(
                acceptance_summary_digest,
                "insight_evaluation_case_summary_digest_invalid",
            ),
            "acceptance_status": acceptance_status,
            "case_id": _required_string(
                case_id,
                "insight_evaluation_case_id_invalid",
            ),
            "question_family": normalized_question_family,
            "variant": _required_string(
                variant,
                "insight_evaluation_case_variant_invalid",
            ),
            "user_message": _required_string(
                user_message,
                "insight_evaluation_case_user_message_invalid",
            ),
            "review_focus": _required_string(
                review_focus,
                "insight_evaluation_case_review_focus_invalid",
            ),
            "run_attempt_id": _required_string(
                run_attempt_id,
                "insight_evaluation_case_run_attempt_id_invalid",
            ),
            "publication_ref": _required_string(
                publication_ref,
                "insight_evaluation_case_publication_ref_invalid",
            ),
            "publication_digest": _digest_string(
                publication_digest,
                "insight_evaluation_case_publication_digest_invalid",
            ),
            "customer_payload_ref": _required_string(
                customer_payload_ref,
                "insight_evaluation_case_customer_payload_ref_invalid",
            ),
            "customer_payload_digest": _digest_string(
                customer_payload_digest,
                "insight_evaluation_case_customer_payload_digest_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            case_snapshot_ref="insight-evaluation-case:sha256:" + digest,
            case_snapshot_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "InsightEvaluationCaseSnapshot":
        payload = _strict_shape(
            payload,
            cls,
            "insight_evaluation_case_shape_invalid",
        )
        rebuilt = cls.create(
            **{
                field: payload[field]
                for field in cls.__dataclass_fields__
                if field not in {"case_snapshot_ref", "case_snapshot_digest"}
            }
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise InsightQualityRubricContractError(
                "insight_evaluation_case_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class InsightModelProfileSnapshot:
    model_profile_ref: str
    source_publication_ref: str
    source_publication_digest: str
    source_narrative_id: str
    source_narrative_attempt_id: str
    writer_attempt_ref: str
    writer_attempt_digest: str
    writer_input_ref: str
    writer_input_digest: str
    writer_attempt_number: int
    provider_ref: str
    model_ref: str
    provider_response_ref: str
    provider_response_digest: str
    model_profile_digest: str

    @classmethod
    def create(
        cls,
        *,
        source_publication_ref: str,
        source_publication_digest: str,
        source_narrative_id: str,
        source_narrative_attempt_id: str,
        writer_attempt_ref: str,
        writer_attempt_digest: str,
        writer_input_ref: str,
        writer_input_digest: str,
        writer_attempt_number: int,
        provider_ref: str,
        model_ref: str,
        provider_response_ref: str,
        provider_response_digest: str,
    ) -> "InsightModelProfileSnapshot":
        if type(writer_attempt_number) is not int or writer_attempt_number <= 0:
            raise InsightQualityRubricContractError(
                "insight_model_profile_attempt_number_invalid"
            )
        body = {
            "source_publication_ref": _required_string(
                source_publication_ref,
                "insight_model_profile_publication_ref_invalid",
            ),
            "source_publication_digest": _digest_string(
                source_publication_digest,
                "insight_model_profile_publication_digest_invalid",
            ),
            "source_narrative_id": _required_string(
                source_narrative_id,
                "insight_model_profile_narrative_id_invalid",
            ),
            "source_narrative_attempt_id": _required_string(
                source_narrative_attempt_id,
                "insight_model_profile_narrative_attempt_id_invalid",
            ),
            "writer_attempt_ref": _required_string(
                writer_attempt_ref,
                "insight_model_profile_writer_attempt_ref_invalid",
            ),
            "writer_attempt_digest": _digest_string(
                writer_attempt_digest,
                "insight_model_profile_writer_attempt_digest_invalid",
            ),
            "writer_input_ref": _required_string(
                writer_input_ref,
                "insight_model_profile_writer_input_ref_invalid",
            ),
            "writer_input_digest": _digest_string(
                writer_input_digest,
                "insight_model_profile_writer_input_digest_invalid",
            ),
            "writer_attempt_number": writer_attempt_number,
            "provider_ref": _required_string(
                provider_ref,
                "insight_model_profile_provider_ref_invalid",
            ),
            "model_ref": _required_string(
                model_ref,
                "insight_model_profile_model_ref_invalid",
            ),
            "provider_response_ref": _required_string(
                provider_response_ref,
                "insight_model_profile_provider_response_ref_invalid",
            ),
            "provider_response_digest": _digest_string(
                provider_response_digest,
                "insight_model_profile_provider_response_digest_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            model_profile_ref="insight-model-profile:sha256:" + digest,
            model_profile_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "InsightModelProfileSnapshot":
        payload = _strict_shape(
            payload,
            cls,
            "insight_model_profile_shape_invalid",
        )
        rebuilt = cls.create(
            **{
                field: payload[field]
                for field in cls.__dataclass_fields__
                if field not in {"model_profile_ref", "model_profile_digest"}
            }
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise InsightQualityRubricContractError(
                "insight_model_profile_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


def human_reason_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(INSIGHT_QUALITY_DIMENSIONS):
        raise InsightQualityRubricContractError("insight_quality_human_reasons_invalid")
    normalized = {
        dimension: _required_string(
            value[dimension],
            "insight_quality_human_reasons_invalid",
        )
        for dimension in INSIGHT_QUALITY_DIMENSIONS
    }
    return MappingProxyType(normalized)


def _strict_shape(
    payload: Any,
    record_type: type,
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise InsightQualityRubricContractError(error)
    return payload


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InsightQualityRubricContractError(error)
    return value


def _digest_string(value: Any, error: str) -> str:
    normalized = _required_string(value, error)
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise InsightQualityRubricContractError(error)
    return normalized


_V1_DIMENSION_RUBRICS = (
    {
        "dimension": "explanation_value",
        "label": "Explanation value",
        "score_1_anchor": (
            "Restates observed values without explaining the business pattern or its "
            "evidence-supported mechanism."
        ),
        "score_3_anchor": (
            "Connects the main pattern to supported drivers, while leaving material "
            "parts of the explanation unresolved."
        ),
        "score_5_anchor": (
            "Builds a coherent, evidence-bounded explanation that accounts for the "
            "main pattern, material exceptions, and known limits."
        ),
        "score_2_interpolation": (
            "Use 2 when the answer adds some explanatory connection beyond score 1 "
            "but does not reach the supported-driver standard of score 3."
        ),
        "score_4_interpolation": (
            "Use 4 when the explanation exceeds score 3 on coherence and coverage but "
            "does not fully meet the exception-and-boundary standard of score 5."
        ),
    },
    {
        "dimension": "novelty",
        "label": "Novelty",
        "score_1_anchor": (
            "Repeats facts already obvious from the question or evidence display."
        ),
        "score_3_anchor": (
            "Surfaces at least one supported relationship or exception that is useful "
            "and not immediately obvious."
        ),
        "score_5_anchor": (
            "Reveals multiple material, non-obvious patterns whose novelty remains "
            "grounded in the accepted evidence."
        ),
        "score_2_interpolation": (
            "Use 2 when there is a modest non-obvious observation but it has limited "
            "materiality or support compared with score 3."
        ),
        "score_4_interpolation": (
            "Use 4 when novelty is broad and material but falls short of the multiple "
            "well-supported patterns required for score 5."
        ),
    },
    {
        "dimension": "decision_usefulness",
        "label": "Decision usefulness",
        "score_1_anchor": (
            "Leaves the reader without a clearer decision, prioritization, or next "
            "investigation choice."
        ),
        "score_3_anchor": (
            "Clarifies a plausible decision or priority and links it to the available "
            "evidence."
        ),
        "score_5_anchor": (
            "Changes or sharpens a material decision with explicit trade-offs, scope, "
            "and evidence boundaries."
        ),
        "score_2_interpolation": (
            "Use 2 when a decision implication is present but remains vague or weakly "
            "connected to evidence relative to score 3."
        ),
        "score_4_interpolation": (
            "Use 4 when the implication is concrete and material but does not fully "
            "specify the trade-offs and boundaries required for score 5."
        ),
    },
    {
        "dimension": "competing_hypotheses",
        "label": "Competing hypotheses",
        "score_1_anchor": (
            "Presents one explanation as settled without considering credible "
            "alternatives allowed by the evidence."
        ),
        "score_3_anchor": (
            "Names material alternatives and distinguishes what the current evidence "
            "supports, weakens, or leaves open."
        ),
        "score_5_anchor": (
            "Compares the strongest alternatives, identifies discriminating evidence, "
            "and calibrates conclusions without suppressing useful hypotheses."
        ),
        "score_2_interpolation": (
            "Use 2 when an alternative is acknowledged but not meaningfully compared "
            "with the leading explanation as required for score 3."
        ),
        "score_4_interpolation": (
            "Use 4 when alternatives are well compared but the discriminating evidence "
            "or remaining ambiguity is less complete than score 5."
        ),
    },
    {
        "dimension": "uncertainty_handling",
        "label": "Uncertainty handling",
        "score_1_anchor": (
            "Uses certainty or causal strength that exceeds the accepted evidence."
        ),
        "score_3_anchor": (
            "States material limitations and calibrates claim strength to the evidence."
        ),
        "score_5_anchor": (
            "Makes uncertainty decision-useful by separating knowns, unknowns, plausible "
            "inferences, and evidence that would resolve them."
        ),
        "score_2_interpolation": (
            "Use 2 when some caveats appear but material overstatement or ambiguity "
            "remains relative to score 3."
        ),
        "score_4_interpolation": (
            "Use 4 when uncertainty is well calibrated but does not fully organize the "
            "knowns, unknowns, and resolution path required for score 5."
        ),
    },
    {
        "dimension": "actionability",
        "label": "Actionability",
        "score_1_anchor": (
            "Offers no feasible action or suggests action disconnected from the evidence."
        ),
        "score_3_anchor": (
            "Proposes a feasible next action with a clear link to the supported finding."
        ),
        "score_5_anchor": (
            "Provides a prioritized, testable action with owner-relevant scope, success "
            "signal, and safeguards for the evidence boundary."
        ),
        "score_2_interpolation": (
            "Use 2 when an action is directionally relevant but underspecified or only "
            "partly supported compared with score 3."
        ),
        "score_4_interpolation": (
            "Use 4 when the action is concrete and testable but lacks one part of the "
            "priority, success-signal, or safeguard standard of score 5."
        ),
    },
)
