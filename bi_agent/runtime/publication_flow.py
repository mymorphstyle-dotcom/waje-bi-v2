from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import AuthorityBundle, RecommendationRecord
from bi_agent.runtime.claim_settlement import (
    AuthorityBundleInputs,
    ClaimSettlement,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_workflow import (
    NarrativeWorkflowResult,
    validate_typed_narrative_workflow_result,
)
from bi_agent.runtime.publication_authority import (
    DeliveryOutboxRecord,
    PublicationProjection,
    PublicationRevision,
)


class PublicationFlowError(ValueError):
    pass


_WRITER_CONTRACT_CUSTOMER_WARNING = (
    "部分分析要求的表达仍需人工复核，当前内容可作为业务判断参考。"
)


def _customer_warnings(
    narrative_workflow: NarrativeWorkflowResult,
    warnings: Sequence[str],
) -> tuple[str, ...]:
    combined = list(warnings)
    if narrative_workflow.writer_contract_findings:
        combined.append(_WRITER_CONTRACT_CUSTOMER_WARNING)
    return tuple(dict.fromkeys(combined))


def _strict_mapping(
    value: Any,
    fields: frozenset[str],
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PublicationFlowError(error)
    return value


def validate_publication_authority_context(
    *,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
) -> AuthorityBundleInputs:
    if type(authority_inputs) is not AuthorityBundleInputs:
        raise PublicationFlowError("publication_flow_authority_inputs_invalid")
    try:
        inputs = AuthorityBundleInputs.create(
            execution_result=authority_inputs.execution_result,
            claim_settlement=authority_inputs.claim_settlement,
            recommendations=authority_inputs.recommendations,
        )
        bundle = AuthorityBundle.from_dict(
            authority_bundle.to_dict(),
            authority_inputs=inputs,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationFlowError("publication_flow_authority_inputs_invalid") from exc
    recommendation_records = tuple(recommendations)
    if (
        any(type(item) is not RecommendationRecord for item in recommendation_records)
        or inputs != authority_inputs
        or bundle != authority_bundle
        or inputs.claim_settlement != claim_settlement
        or len({item.recommendation_ref for item in recommendation_records})
        != len(recommendation_records)
        or tuple(
            sorted(
                recommendation_records,
                key=lambda item: item.recommendation_ref,
            )
        )
        != inputs.recommendations
    ):
        raise PublicationFlowError("publication_flow_authority_inputs_invalid")
    return inputs


def _material_projection_evidence_entries(
    authority_inputs: AuthorityBundleInputs,
) -> tuple[EvidenceLedgerEntry, ...]:
    try:
        return authority_inputs.material_projection_evidence_entries()
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationFlowError(
            "publication_flow_material_projection_evidence_incomplete"
        ) from exc


@dataclass(frozen=True)
class ValidatedPublicationFlowContext:
    authority_inputs: AuthorityBundleInputs
    authority_bundle: AuthorityBundle
    claim_settlement: ClaimSettlement
    recommendations: tuple[RecommendationRecord, ...]
    narrative_workflow: NarrativeWorkflowResult


def validate_publication_flow_context(
    *,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    narrative_workflow: NarrativeWorkflowResult,
) -> ValidatedPublicationFlowContext:
    inputs = validate_publication_authority_context(
        authority_inputs=authority_inputs,
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        recommendations=recommendations,
    )
    normalized_recommendations = tuple(
        sorted(recommendations, key=lambda item: item.recommendation_ref)
    )
    try:
        workflow = validate_typed_narrative_workflow_result(
            narrative_workflow,
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            evidence_entries=_material_projection_evidence_entries(inputs),
            recommendations=normalized_recommendations,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationFlowError("publication_flow_narrative_invalid") from exc
    if tuple(item.recommendation_ref for item in normalized_recommendations) != tuple(
        item.recommendation_ref for item in workflow.material_projection.recommendations
    ):
        raise PublicationFlowError("publication_flow_recommendations_invalid")
    return ValidatedPublicationFlowContext(
        authority_inputs=inputs,
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        recommendations=normalized_recommendations,
        narrative_workflow=workflow,
    )


@dataclass(frozen=True)
class PublicationFlowResult:
    flow_ref: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    narrative_workflow_ref: str
    narrative_workflow_digest: str
    projection: PublicationProjection
    publication: PublicationRevision
    outbox: DeliveryOutboxRecord
    customer_payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_inputs: AuthorityBundleInputs,
        authority_bundle: AuthorityBundle,
        claim_settlement: ClaimSettlement,
        recommendations: Sequence[RecommendationRecord],
        narrative_workflow: NarrativeWorkflowResult,
        supersedes_publication: PublicationRevision | None,
        destination_ref: str,
        channel: str,
        published_at: str | datetime,
        customer_term_labels: Mapping[str, str] | None = None,
        visualization_refs: Sequence[str] = (),
        warnings: Sequence[str] = (),
    ) -> "PublicationFlowResult":
        context = validate_publication_flow_context(
            authority_inputs=authority_inputs,
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            recommendations=recommendations,
            narrative_workflow=narrative_workflow,
        )
        return cls._create_validated(
            context=context,
            supersedes_publication=supersedes_publication,
            destination_ref=destination_ref,
            channel=channel,
            published_at=published_at,
            customer_term_labels=customer_term_labels,
            visualization_refs=visualization_refs,
            warnings=warnings,
        )

    @classmethod
    def _create_validated(
        cls,
        *,
        context: ValidatedPublicationFlowContext,
        supersedes_publication: PublicationRevision | None,
        destination_ref: str,
        channel: str,
        published_at: str | datetime,
        customer_term_labels: Mapping[str, str] | None,
        visualization_refs: Sequence[str],
        warnings: Sequence[str],
    ) -> "PublicationFlowResult":
        authority_bundle = context.authority_bundle
        narrative_result = context.narrative_workflow
        narrative = narrative_result.final_accepted_narrative
        if narrative is None:
            raise PublicationFlowError("publication_flow_narrative_not_publishable")
        local_report = narrative_result.final_local_report
        verifier_report = narrative_result.projection_ready_verifier_report
        safety_excluded = {
            finding.block_id for finding in local_report.sensitive_output_findings
        }
        display_order = tuple(
            block.block_id
            for block in narrative.blocks
            if block.block_id not in safety_excluded
        )
        projection = PublicationProjection.create(
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            visibility_policy=narrative_result.visibility_policy,
            display_order=display_order,
            customer_term_labels=customer_term_labels,
            visualization_refs=visualization_refs,
            warnings=_customer_warnings(narrative_result, warnings),
        )
        customer_payload = projection.to_customer_payload(
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            visibility_policy=narrative_result.visibility_policy,
        )
        publication = PublicationRevision.create(
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            projection=projection,
            visibility_policy=narrative_result.visibility_policy,
            revision=(
                1
                if supersedes_publication is None
                else supersedes_publication.revision + 1
            ),
            supersedes_publication=supersedes_publication,
            published_at=published_at,
        )
        outbox = DeliveryOutboxRecord.enqueue(
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            visibility_policy=narrative_result.visibility_policy,
            supersedes_publication=supersedes_publication,
            publication=publication,
            projection=projection,
            destination_ref=destination_ref,
            channel=channel,
        )
        return cls._assemble_validated(
            context=context,
            projection=projection,
            publication=publication,
            outbox=outbox,
            customer_payload=customer_payload,
        )

    @classmethod
    def _assemble_validated(
        cls,
        *,
        context: ValidatedPublicationFlowContext,
        projection: PublicationProjection,
        publication: PublicationRevision,
        outbox: DeliveryOutboxRecord,
        customer_payload: Mapping[str, Any],
    ) -> "PublicationFlowResult":
        authority_bundle = context.authority_bundle
        narrative_result = context.narrative_workflow
        body = {
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "narrative_workflow_ref": (
                "narrative-workflow-result:sha256:" + narrative_result.content_digest
            ),
            "narrative_workflow_digest": narrative_result.content_digest,
            "projection_ref": projection.projection_id,
            "projection_digest": projection.projection_digest,
            "publication_ref": publication.publication_ref,
            "publication_digest": publication.publication_digest,
            "outbox_ref": outbox.outbox_ref,
            "outbox_digest": outbox.content_digest,
            "customer_payload_digest": canonical_digest(customer_payload),
        }
        digest = canonical_digest(body)
        return cls(
            flow_ref="publication-flow:sha256:" + digest,
            authority_bundle_ref=authority_bundle.bundle_ref,
            authority_bundle_digest=authority_bundle.bundle_digest,
            narrative_workflow_ref=(
                "narrative-workflow-result:sha256:" + narrative_result.content_digest
            ),
            narrative_workflow_digest=narrative_result.content_digest,
            projection=projection,
            publication=publication,
            outbox=outbox,
            customer_payload=canonical_value(customer_payload),
            content_digest=digest,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_inputs: AuthorityBundleInputs,
        authority_bundle: AuthorityBundle,
        claim_settlement: ClaimSettlement,
        recommendations: Sequence[RecommendationRecord],
        narrative_workflow: NarrativeWorkflowResult,
        supersedes_publication: PublicationRevision | None,
    ) -> "PublicationFlowResult":
        context = validate_publication_flow_context(
            authority_inputs=authority_inputs,
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            recommendations=recommendations,
            narrative_workflow=narrative_workflow,
        )
        return cls._from_dict_validated(
            payload,
            context=context,
            supersedes_publication=supersedes_publication,
        )

    @classmethod
    def _from_dict_validated(
        cls,
        payload: Mapping[str, Any],
        *,
        context: ValidatedPublicationFlowContext,
        supersedes_publication: PublicationRevision | None,
    ) -> "PublicationFlowResult":
        payload = _strict_mapping(
            payload,
            frozenset(cls.__dataclass_fields__),
            "publication_flow_shape_invalid",
        )
        raw_projection = payload["projection"]
        raw_publication = payload["publication"]
        raw_outbox = payload["outbox"]
        raw_customer_payload = payload["customer_payload"]
        if not all(
            isinstance(item, Mapping)
            for item in (raw_projection, raw_publication, raw_outbox)
        ) or not isinstance(raw_customer_payload, Mapping):
            raise PublicationFlowError("publication_flow_children_invalid")
        authority_bundle = context.authority_bundle
        narrative_result = context.narrative_workflow
        narrative = narrative_result.final_accepted_narrative
        if narrative is None:
            raise PublicationFlowError("publication_flow_narrative_not_publishable")
        local_report = narrative_result.final_local_report
        verifier_report = narrative_result.projection_ready_verifier_report
        projection = PublicationProjection.from_dict(
            raw_projection,
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            visibility_policy=narrative_result.visibility_policy,
        )
        publication = PublicationRevision.from_dict(
            raw_publication,
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            projection=projection,
            visibility_policy=narrative_result.visibility_policy,
            supersedes_publication=supersedes_publication,
        )
        outbox = DeliveryOutboxRecord.from_dict(
            raw_outbox,
            publication=publication,
            projection=projection,
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            visibility_policy=narrative_result.visibility_policy,
            supersedes_publication=supersedes_publication,
        )
        customer_payload = projection.to_customer_payload(
            authority_bundle=authority_bundle,
            material_projection=narrative_result.material_projection,
            narrative=narrative,
            local_report=local_report,
            verifier_report=verifier_report,
            visibility_policy=narrative_result.visibility_policy,
        )
        expected = cls._assemble_validated(
            context=context,
            projection=projection,
            publication=publication,
            outbox=outbox,
            customer_payload=customer_payload,
        )
        if expected.to_dict() != canonical_value(payload):
            raise PublicationFlowError("publication_flow_integrity_invalid")
        return expected

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


def validate_typed_publication_flow(
    value: PublicationFlowResult,
    *,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    narrative_workflow: NarrativeWorkflowResult,
    supersedes_publication: PublicationRevision | None,
) -> PublicationFlowResult:
    context = validate_publication_flow_context(
        authority_inputs=authority_inputs,
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        recommendations=recommendations,
        narrative_workflow=narrative_workflow,
    )
    return validate_publication_flow_in_context(
        value,
        context=context,
        supersedes_publication=supersedes_publication,
    )


def validate_publication_flow_in_context(
    value: PublicationFlowResult,
    *,
    context: ValidatedPublicationFlowContext,
    supersedes_publication: PublicationRevision | None,
) -> PublicationFlowResult:
    if type(value) is not PublicationFlowResult:
        raise PublicationFlowError("publication_flow_invalid")
    replayed = PublicationFlowResult._from_dict_validated(
        value.to_dict(),
        context=context,
        supersedes_publication=supersedes_publication,
    )
    if replayed != value:
        raise PublicationFlowError("publication_flow_integrity_invalid")
    return value


__all__ = (
    "PublicationFlowError",
    "PublicationFlowResult",
    "ValidatedPublicationFlowContext",
    "validate_publication_authority_context",
    "validate_publication_flow_context",
    "validate_publication_flow_in_context",
    "validate_typed_publication_flow",
)
