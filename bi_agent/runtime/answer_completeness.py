from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import NarrativeDocument
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
)


ANSWER_COMPLETENESS_SCHEMA_VERSION = "answer-completeness-assessment.v2"
_REQUIREMENT_STATUSES = frozenset(
    {"satisfied", "mixed", "contradicted", "unavailable"}
)


class AnswerCompletenessContractError(ValueError):
    pass


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AnswerCompletenessContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AnswerCompletenessContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if (not allow_empty and not normalized) or len(normalized) != len(
        set(normalized)
    ):
        raise AnswerCompletenessContractError(error)
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class RequirementCompletenessGap:
    gap_ref: str
    requirement_handle: str
    requirement_status: str
    claim_kind: str
    missing_claim_handle_options: tuple[str, ...]
    missing_fact_handles: tuple[str, ...]
    missing_limitation_handles: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        requirement_handle: str,
        requirement_status: str,
        claim_kind: str,
        missing_claim_handle_options: Sequence[str],
        missing_fact_handles: Sequence[str],
        missing_limitation_handles: Sequence[str],
    ) -> "RequirementCompletenessGap":
        status = _required_string(
            requirement_status,
            "answer_completeness_requirement_status_invalid",
        )
        if status not in _REQUIREMENT_STATUSES:
            raise AnswerCompletenessContractError(
                "answer_completeness_requirement_status_invalid"
            )
        missing_claims = _string_tuple(
            missing_claim_handle_options,
            "answer_completeness_missing_claim_handles_invalid",
        )
        missing_limitations = _string_tuple(
            missing_limitation_handles,
            "answer_completeness_missing_limitation_handles_invalid",
        )
        missing_facts = _string_tuple(
            missing_fact_handles,
            "answer_completeness_missing_fact_handles_invalid",
        )
        if not missing_claims and not missing_facts and not missing_limitations:
            raise AnswerCompletenessContractError(
                "answer_completeness_empty_gap_invalid"
            )
        if status == "unavailable" and (missing_claims or missing_facts):
            raise AnswerCompletenessContractError(
                "answer_completeness_unavailable_claim_gap_invalid"
            )
        body = {
            "requirement_handle": _required_string(
                requirement_handle,
                "answer_completeness_requirement_handle_invalid",
            ),
            "requirement_status": status,
            "claim_kind": _required_string(
                claim_kind,
                "answer_completeness_claim_kind_invalid",
            ),
            "missing_claim_handle_options": missing_claims,
            "missing_fact_handles": missing_facts,
            "missing_limitation_handles": missing_limitations,
        }
        digest = canonical_digest(body)
        return cls(
            gap_ref="answer-requirement-gap:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RequirementCompletenessGap":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise AnswerCompletenessContractError(
                "answer_completeness_gap_shape_invalid"
            )
        rebuilt = cls.create(
            requirement_handle=payload["requirement_handle"],
            requirement_status=payload["requirement_status"],
            claim_kind=payload["claim_kind"],
            missing_claim_handle_options=payload["missing_claim_handle_options"],
            missing_fact_handles=payload["missing_fact_handles"],
            missing_limitation_handles=payload["missing_limitation_handles"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise AnswerCompletenessContractError(
                "answer_completeness_gap_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class AnswerCompletenessAssessment:
    assessment_ref: str
    schema_version: str
    material_projection_ref: str
    material_projection_digest: str
    source_narrative_id: str
    source_narrative_digest: str
    required_block_ids: tuple[str, ...]
    requirement_handles: tuple[str, ...]
    satisfied_requirement_handles: tuple[str, ...]
    gaps: tuple[RequirementCompletenessGap, ...]
    status: str
    content_digest: str

    @classmethod
    def evaluate(
        cls,
        *,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
    ) -> "AnswerCompletenessAssessment":
        if type(material_projection) is not NarrativeMaterialProjection:
            raise AnswerCompletenessContractError(
                "answer_completeness_projection_invalid"
            )
        if type(narrative) is not NarrativeDocument:
            raise AnswerCompletenessContractError(
                "answer_completeness_narrative_invalid"
            )
        try:
            material_projection.assert_integrity()
            replayed_narrative = NarrativeDocument.from_dict(narrative.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise AnswerCompletenessContractError(
                "answer_completeness_authority_invalid"
            ) from exc
        if replayed_narrative != narrative or (
            narrative.material_projection_ref != material_projection.projection_ref
            or narrative.material_projection_digest
            != material_projection.content_digest
        ):
            raise AnswerCompletenessContractError(
                "answer_completeness_authority_invalid"
            )
        required_blocks = tuple(block for block in narrative.blocks if block.required)
        claim_handles = frozenset(
            handle for block in required_blocks for handle in block.claim_handles
        )
        limitation_handles = frozenset(
            handle
            for block in required_blocks
            for handle in block.limitation_handles
        )
        fact_binding_pairs = frozenset(
            (binding.claim_handle, binding.fact_handle)
            for block in required_blocks
            for binding in block.material_fact_bindings
        )
        gaps: list[RequirementCompletenessGap] = []
        satisfied: list[str] = []
        for requirement in material_projection.publication_requirements:
            if requirement.status not in _REQUIREMENT_STATUSES:
                raise AnswerCompletenessContractError(
                    "answer_completeness_requirement_status_invalid"
                )
            missing_claims = (
                requirement.claim_handles
                if requirement.status in {"satisfied", "mixed", "contradicted"}
                and claim_handles.isdisjoint(requirement.claim_handles)
                else ()
            )
            missing_limitations = tuple(
                handle
                for handle in requirement.limitation_handles
                if handle not in limitation_handles
            )
            missing_facts = tuple(
                fact_handle
                for fact_handle in requirement.required_fact_handles
                if not any(
                    (claim_handle, fact_handle) in fact_binding_pairs
                    for claim_handle in requirement.claim_handles
                )
            )
            if missing_claims or missing_facts or missing_limitations:
                gaps.append(
                    RequirementCompletenessGap.create(
                        requirement_handle=requirement.requirement_handle,
                        requirement_status=requirement.status,
                        claim_kind=requirement.claim_kind,
                        missing_claim_handle_options=missing_claims,
                        missing_fact_handles=missing_facts,
                        missing_limitation_handles=missing_limitations,
                    )
                )
            else:
                satisfied.append(requirement.requirement_handle)
        gap_records = tuple(sorted(gaps, key=lambda item: item.requirement_handle))
        requirement_handles = tuple(
            sorted(
                item.requirement_handle
                for item in material_projection.publication_requirements
            )
        )
        body = {
            "schema_version": ANSWER_COMPLETENESS_SCHEMA_VERSION,
            "material_projection_ref": material_projection.projection_ref,
            "material_projection_digest": material_projection.content_digest,
            "source_narrative_id": narrative.narrative_id,
            "source_narrative_digest": narrative.content_digest,
            "required_block_ids": tuple(
                sorted(block.block_id for block in required_blocks)
            ),
            "requirement_handles": requirement_handles,
            "satisfied_requirement_handles": tuple(sorted(satisfied)),
            "gaps": gap_records,
            "status": "complete" if not gap_records else "incomplete",
        }
        digest = canonical_digest(body)
        return cls(
            assessment_ref="answer-completeness-assessment:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
    ) -> "AnswerCompletenessAssessment":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise AnswerCompletenessContractError(
                "answer_completeness_assessment_shape_invalid"
            )
        rebuilt = cls.evaluate(
            material_projection=material_projection,
            narrative=narrative,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise AnswerCompletenessContractError(
                "answer_completeness_assessment_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


__all__ = (
    "ANSWER_COMPLETENESS_SCHEMA_VERSION",
    "AnswerCompletenessAssessment",
    "AnswerCompletenessContractError",
    "RequirementCompletenessGap",
)
