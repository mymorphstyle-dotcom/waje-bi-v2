from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimPublicationCeiling,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.insight_quality_rubric import (
    INSIGHT_QUALITY_DIMENSIONS,
    InsightEvaluationCaseSnapshot,
    InsightModelProfileSnapshot,
    InsightQualityRubric,
    InsightQualityRubricContractError,
    human_reason_mapping,
)
from bi_agent.runtime.narrative_authority import (
    BlockLocalValidationReport,
    NarrativeDocument,
    NarrativeFactBinding,
    NarrativeAuthorityContractError,
    PublicationFieldVisibilityPolicy,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
    NarrativeMaterialProjectionContractError,
    ProjectedEvidenceFact,
)
from bi_agent.runtime.single_authority import LifecycleState


class PublicationAuthorityContractError(ValueError):
    pass


DELIVERY_ATTEMPT_STATUSES = frozenset(
    {"published", "retryable_failed", "permanently_failed"}
)
_CUSTOMER_TERM_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PublicationAuthorityContractError(error)
    return value


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _integer(value: Any, error: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise PublicationAuthorityContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PublicationAuthorityContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise PublicationAuthorityContractError(error)
    if len(normalized) != len(set(normalized)):
        raise PublicationAuthorityContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _strict_shape(
    payload: Any,
    record_type: type,
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise PublicationAuthorityContractError(error)
    return payload


def _aware_iso(value: str | datetime, error: str) -> str:
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
    except ValueError as exc:
        raise PublicationAuthorityContractError(error) from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise PublicationAuthorityContractError(error)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ceiling_mapping(
    value: Mapping[str, ClaimPublicationCeiling],
    error: str,
) -> Mapping[str, ClaimPublicationCeiling]:
    if not isinstance(value, Mapping):
        raise PublicationAuthorityContractError(error)
    normalized: dict[str, ClaimPublicationCeiling] = {}
    for raw_ref, ceiling in value.items():
        ref = _required_string(raw_ref, error)
        if type(ceiling) is not ClaimPublicationCeiling:
            raise PublicationAuthorityContractError(error)
        normalized[ref] = ceiling
    return MappingProxyType(dict(sorted(normalized.items())))


def _customer_term_label_mapping(
    value: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise PublicationAuthorityContractError(
            "publication_projection_customer_term_labels_invalid"
        )
    normalized: dict[str, str] = {}
    for raw_term, raw_label in value.items():
        term = _required_string(
            raw_term,
            "publication_projection_customer_term_labels_invalid",
        )
        label = _required_string(
            raw_label,
            "publication_projection_customer_term_labels_invalid",
        )
        if not _CUSTOMER_TERM_PATTERN.fullmatch(term):
            raise PublicationAuthorityContractError(
                "publication_projection_customer_term_labels_invalid"
            )
        normalized[term] = label
    return MappingProxyType(dict(sorted(normalized.items())))


def _project_customer_terms(text: str, labels: Mapping[str, str]) -> str:
    projected = text
    for term, label in sorted(labels.items(), key=lambda item: (-len(item[0]), item[0])):
        projected = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            lambda _match, replacement=label: replacement,
            projected,
        )
    return projected


def _scores_mapping(value: Mapping[str, int]) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(INSIGHT_QUALITY_DIMENSIONS):
        raise PublicationAuthorityContractError("insight_quality_scores_invalid")
    normalized: dict[str, int] = {}
    for dimension in INSIGHT_QUALITY_DIMENSIONS:
        score = value[dimension]
        if type(score) is not int or not 1 <= score <= 5:
            raise PublicationAuthorityContractError("insight_quality_scores_invalid")
        normalized[dimension] = score
    return MappingProxyType(normalized)


def _assert_content_addressed_record(
    record: Any,
    *,
    ref_field: str,
    digest_field: str,
    prefix: str,
    excluded_fields: Sequence[str] = (),
    error: str,
) -> None:
    if not hasattr(record, "to_dict"):
        raise PublicationAuthorityContractError(error)
    payload = record.to_dict()
    excluded = {ref_field, digest_field, *excluded_fields}
    digest = canonical_digest(
        {key: value for key, value in payload.items() if key not in excluded}
    )
    ref = payload.get(ref_field)
    namespaced_prefix = prefix.removesuffix("sha256:")
    ref_valid = ref == prefix + digest or (
        isinstance(ref, str)
        and ref.startswith(namespaced_prefix)
        and ref.endswith(":sha256:" + digest)
    )
    if payload.get(digest_field) != digest or not ref_valid:
        raise PublicationAuthorityContractError(error)


def _assert_authority_bundle_integrity(
    bundle: AuthorityBundle,
    *,
    error: str,
) -> None:
    if type(bundle) is not AuthorityBundle:
        raise PublicationAuthorityContractError(error)
    manifest = {
        "bundle_revision": bundle.bundle_revision,
        "supersedes_bundle_ref": bundle.supersedes_bundle_ref,
        "run_attempt_id": bundle.run_attempt_id,
        "intent_revision_id": bundle.intent_revision_id,
        "decision_refs": bundle.decision_refs,
        "plan_revision_id": bundle.plan_revision_id,
        "authority_context_ref": bundle.authority_context_ref,
        "execution_result_ref": bundle.execution_result_ref,
        "execution_result_digest": bundle.execution_result_digest,
        "claim_settlement_ref": bundle.claim_settlement_ref,
        "claim_settlement_digest": bundle.claim_settlement_digest,
        "claim_graph_ref": bundle.claim_graph_ref,
        "claim_graph_digest": bundle.claim_graph_digest,
        "authority_mode": bundle.authority_mode,
        "required_obligation_ids": bundle.required_obligation_ids,
        "obligation_coverage_refs": bundle.obligation_coverage_refs,
        "evidence_refs": bundle.evidence_refs,
        "verified_claim_refs": bundle.verified_claim_refs,
        "recommendation_refs": bundle.recommendation_refs,
        "assumption_refs": bundle.assumption_refs,
        "limitation_refs": bundle.limitation_refs,
        "claim_verifier_report_ref": bundle.claim_verifier_report_ref,
    }
    digest = canonical_digest(manifest)
    namespace_prefix = "claim-authority-namespace:sha256:"
    if not bundle.authority_namespace_ref.startswith(namespace_prefix):
        raise PublicationAuthorityContractError(error)
    namespace_token = bundle.authority_namespace_ref.removeprefix(namespace_prefix)[:24]
    if (
        bundle.bundle_digest != digest
        or bundle.content_digest != digest
        or bundle.seal_state != "sealed"
        or bundle.bundle_ref != f"authority-bundle:{namespace_token}:sha256:{digest}"
        or bundle.authority_mode not in {"claim_bearing", "boundary_only"}
        or (bundle.authority_mode == "claim_bearing" and not bundle.verified_claim_refs)
        or (
            bundle.authority_mode == "boundary_only"
            and (
                bundle.verified_claim_refs
                or bundle.recommendation_refs
                or not bundle.obligation_coverage_refs
                or not bundle.limitation_refs
            )
        )
    ):
        raise PublicationAuthorityContractError(error)


def _replay_projection_sources(
    *,
    authority_bundle: AuthorityBundle,
    material_projection: NarrativeMaterialProjection,
    narrative: NarrativeDocument,
    local_report: BlockLocalValidationReport,
    visibility_policy: PublicationFieldVisibilityPolicy,
) -> tuple[
    NarrativeDocument,
    BlockLocalValidationReport,
    PublicationFieldVisibilityPolicy,
]:
    _assert_authority_bundle_integrity(
        authority_bundle,
        error="publication_projection_authority_integrity_invalid",
    )
    if (
        type(material_projection) is not NarrativeMaterialProjection
        or type(narrative) is not NarrativeDocument
        or type(local_report) is not BlockLocalValidationReport
        or type(visibility_policy) is not PublicationFieldVisibilityPolicy
    ):
        raise PublicationAuthorityContractError("publication_projection_source_invalid")
    try:
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
        material_projection.assert_integrity()
        replayed_narrative = NarrativeDocument.from_dict(narrative.to_dict())
        replayed_local = BlockLocalValidationReport.from_dict(
            local_report.to_dict(),
            narrative=replayed_narrative,
            material_projection=material_projection,
            visibility_policy=policy,
        )
    except (
        NarrativeAuthorityContractError,
        NarrativeMaterialProjectionContractError,
    ) as exc:
        raise PublicationAuthorityContractError(
            "publication_projection_source_integrity_invalid"
        ) from exc
    return replayed_narrative, replayed_local, policy


def _projection_authority_indexes(
    material_projection: NarrativeMaterialProjection,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[tuple[str, str], ProjectedEvidenceFact],
]:
    try:
        material_projection.assert_integrity()
    except NarrativeMaterialProjectionContractError as exc:
        raise PublicationAuthorityContractError(
            "publication_projection_source_integrity_invalid"
        ) from exc
    claim_by_handle = {
        claim.claim_handle: claim for claim in material_projection.claims
    }
    recommendation_by_handle = {
        recommendation.recommendation_handle: recommendation
        for recommendation in material_projection.recommendations
    }
    limitation_by_handle = {
        limitation.limitation_handle: limitation
        for limitation in material_projection.limitations
    }
    material_by_handle = {
        material.material_handle: material
        for material in material_projection.evidence_materials
    }
    fact_by_pair: dict[tuple[str, str], ProjectedEvidenceFact] = {}
    for claim in material_projection.claims:
        for material_handle in claim.material_handles:
            material = material_by_handle.get(material_handle)
            if material is None:
                raise PublicationAuthorityContractError(
                    "publication_projection_source_integrity_invalid"
                )
            for fact in material.facts:
                fact_by_pair[(claim.claim_handle, fact.fact_handle)] = fact
    return (
        claim_by_handle,
        recommendation_by_handle,
        limitation_by_handle,
        fact_by_pair,
    )


def _resolve_projection_fact(
    fact_by_pair: Mapping[tuple[str, str], ProjectedEvidenceFact],
    binding: NarrativeFactBinding,
) -> ProjectedEvidenceFact:
    fact = fact_by_pair.get((binding.claim_handle, binding.fact_handle))
    if fact is None or (
        binding.fact_kind != fact.fact_kind
        or binding.value != fact.value
        or binding.range_end != fact.range_end
        or binding.unit != fact.unit
    ):
        raise PublicationAuthorityContractError(
            "publication_projection_fact_binding_invalid"
        )
    return fact


@dataclass(frozen=True)
class PublicationProjection:
    projection_id: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    material_projection_ref: str
    material_projection_digest: str
    narrative_id: str
    narrative_digest: str
    local_report_ref: str
    local_report_digest: str
    field_visibility_policy_ref: str
    field_visibility_policy_digest: str
    published_block_ids: tuple[str, ...]
    safety_excluded_block_ids: tuple[str, ...]
    display_order: tuple[str, ...]
    claim_refs: tuple[str, ...]
    recommendation_refs: tuple[str, ...]
    claim_ceiling_by_ref: Mapping[str, ClaimPublicationCeiling]
    customer_term_labels: Mapping[str, str]
    visualization_refs: tuple[str, ...]
    warnings: tuple[str, ...]
    projection_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        visibility_policy: PublicationFieldVisibilityPolicy,
        display_order: Sequence[str],
        customer_term_labels: Mapping[str, str] | None = None,
        visualization_refs: Sequence[str],
        warnings: Sequence[str],
    ) -> "PublicationProjection":
        narrative, local_report, visibility_policy = (
            _replay_projection_sources(
                authority_bundle=authority_bundle,
                material_projection=material_projection,
                narrative=narrative,
                local_report=local_report,
                visibility_policy=visibility_policy,
            )
        )
        if (
            material_projection.claim_settlement_ref
            != authority_bundle.claim_settlement_ref
            or material_projection.claim_settlement_digest
            != authority_bundle.claim_settlement_digest
            or material_projection.authority_mode != authority_bundle.authority_mode
            or tuple(sorted(item.claim_ref for item in material_projection.claims))
            != tuple(sorted(authority_bundle.verified_claim_refs))
            or tuple(
                sorted(
                    item.recommendation_ref
                    for item in material_projection.recommendations
                )
            )
            != tuple(sorted(authority_bundle.recommendation_refs))
            or tuple(
                sorted(item.limitation_ref for item in material_projection.limitations)
            )
            != tuple(sorted(authority_bundle.limitation_refs))
            or narrative.authority_bundle_ref != authority_bundle.bundle_ref
            or narrative.material_projection_ref != material_projection.projection_ref
            or narrative.material_projection_digest
            != material_projection.content_digest
        ):
            raise PublicationAuthorityContractError(
                "publication_projection_authority_closure_invalid"
            )
        if (
            local_report.narrative_id != narrative.narrative_id
            or local_report.narrative_digest != narrative.content_digest
            or local_report.material_projection_ref
            != material_projection.projection_ref
            or local_report.material_projection_digest
            != material_projection.content_digest
        ):
            raise PublicationAuthorityContractError(
                "publication_projection_narrative_closure_invalid"
            )

        block_by_id = {block.block_id: block for block in narrative.blocks}
        # Business-quality verification is retained as audit evidence and does
        # not decide customer delivery. Only hard sensitive-output findings may
        # remove generated text from the customer projection.
        safety_excluded = tuple(
            sorted(
                {
                    finding.block_id
                    for finding in local_report.sensitive_output_findings
                }
            )
        )
        published = tuple(
            sorted(
                block_id
                for block_id, block in block_by_id.items()
                if block_id not in safety_excluded
            )
        )
        if not published:
            raise PublicationAuthorityContractError(
                "publication_projection_no_safe_blocks"
            )
        ordered = _string_tuple(
            display_order,
            "publication_projection_display_order_invalid",
            allow_empty=False,
            sort=False,
        )
        if set(ordered) != set(published):
            raise PublicationAuthorityContractError(
                "publication_projection_display_order_invalid"
            )

        (
            public_claim_by_handle,
            public_recommendation_by_handle,
            _,
            fact_by_pair,
        ) = _projection_authority_indexes(material_projection)
        published_claim_handles = {
            handle
            for block_id in published
            for handle in block_by_id[block_id].claim_handles
            if handle in public_claim_by_handle
        }
        published_recommendation_handles = {
            handle
            for block_id in published
            for handle in block_by_id[block_id].recommendation_handles
            if handle in public_recommendation_by_handle
        }
        public_claims = tuple(
            sorted(
                (
                    public_claim_by_handle[handle]
                    for handle in published_claim_handles
                ),
                key=lambda item: item.claim_ref,
            )
        )
        public_recommendations = tuple(
            sorted(
                (
                    public_recommendation_by_handle[handle]
                    for handle in published_recommendation_handles
                ),
                key=lambda item: item.recommendation_ref,
            )
        )
        claim_refs = tuple(item.claim_ref for item in public_claims)
        if not set(claim_refs).issubset(set(authority_bundle.verified_claim_refs)):
            raise PublicationAuthorityContractError(
                "publication_projection_claim_authority_invalid"
            )
        recommendation_refs = tuple(
            item.recommendation_ref for item in public_recommendations
        )
        if not set(recommendation_refs).issubset(
            set(authority_bundle.recommendation_refs)
        ):
            raise PublicationAuthorityContractError(
                "publication_projection_recommendation_authority_invalid"
            )
        ceilings = _ceiling_mapping(
            {item.claim_ref: item.publication_ceiling for item in public_claims},
            "publication_projection_claim_ceiling_invalid",
        )
        body = {
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "material_projection_ref": material_projection.projection_ref,
            "material_projection_digest": material_projection.content_digest,
            "narrative_id": narrative.narrative_id,
            "narrative_digest": narrative.content_digest,
            "local_report_ref": local_report.local_report_ref,
            "local_report_digest": local_report.content_digest,
            "field_visibility_policy_ref": visibility_policy.policy_ref,
            "field_visibility_policy_digest": visibility_policy.content_digest,
            "published_block_ids": published,
            "safety_excluded_block_ids": safety_excluded,
            "display_order": ordered,
            "claim_refs": claim_refs,
            "recommendation_refs": recommendation_refs,
            "claim_ceiling_by_ref": ceilings,
            "customer_term_labels": _customer_term_label_mapping(
                customer_term_labels
            ),
            "visualization_refs": _string_tuple(
                visualization_refs,
                "publication_projection_visualization_refs_invalid",
            ),
            "warnings": _string_tuple(
                warnings,
                "publication_projection_warnings_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            projection_id="publication-projection:sha256:" + digest,
            projection_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "PublicationProjection":
        payload = _strict_shape(
            payload,
            cls,
            "publication_projection_shape_invalid",
        )
        rebuilt = cls.create(
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            narrative=narrative,
            local_report=local_report,
            visibility_policy=visibility_policy,
            display_order=payload["display_order"],
            customer_term_labels=payload["customer_term_labels"],
            visualization_refs=payload["visualization_refs"],
            warnings=payload["warnings"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationAuthorityContractError(
                "publication_projection_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_customer_payload(
        self,
        *,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> dict[str, Any]:
        replayed = PublicationProjection.from_dict(
            self.to_dict(),
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            narrative=narrative,
            local_report=local_report,
            visibility_policy=visibility_policy,
        )
        if replayed != self:
            raise PublicationAuthorityContractError(
                "publication_projection_integrity_invalid"
            )
        block_by_id = {block.block_id: block for block in narrative.blocks}
        (
            claim_by_handle,
            recommendation_by_handle,
            limitation_by_handle,
            fact_by_pair,
        ) = _projection_authority_indexes(material_projection)
        published_blocks: list[dict[str, Any]] = []
        published_claim_refs: set[str] = set()
        published_recommendation_refs: set[str] = set()
        limitation_refs: set[str] = set()
        synthesis_block_ids = tuple(
            block_id
            for block_id in self.display_order
            if not block_by_id[block_id].requirement_handles
        )
        customer_block_ids = synthesis_block_ids or self.display_order
        for block_id in customer_block_ids:
            block = block_by_id[block_id]
            block_claim_refs = tuple(
                sorted(
                    claim_by_handle[handle].claim_ref
                    for handle in block.claim_handles
                    if handle in claim_by_handle
                )
            )
            block_recommendation_refs = tuple(
                sorted(
                    recommendation_by_handle[handle].recommendation_ref
                    for handle in block.recommendation_handles
                    if handle in recommendation_by_handle
                )
            )
            block_limitation_refs = tuple(
                sorted(
                    limitation_by_handle[handle].limitation_ref
                    for handle in block.limitation_handles
                    if handle in limitation_by_handle
                )
            )
            published_claim_refs.update(block_claim_refs)
            published_recommendation_refs.update(block_recommendation_refs)
            limitation_refs.update(block_limitation_refs)
            bindings = []
            for binding in block.material_fact_bindings:
                try:
                    fact = _resolve_projection_fact(fact_by_pair, binding)
                except PublicationAuthorityContractError:
                    # Keep the generated business insight visible. The invalid
                    # binding remains in the local report for human review, and
                    # is omitted from the public evidence metadata.
                    continue
                bindings.append(
                    {
                        "name": fact.name,
                        "fact_kind": binding.fact_kind,
                        "value": binding.value,
                        "range_end": binding.range_end,
                        "unit": binding.unit,
                    }
                )
            published_blocks.append(
                {
                    "role": block.role,
                    "text": _project_customer_terms(
                        block.text,
                        self.customer_term_labels,
                    ),
                    "statement_role": block.statement_role,
                    "claim_refs": block_claim_refs,
                    "recommendation_refs": block_recommendation_refs,
                    "limitation_refs": block_limitation_refs,
                    "material_fact_bindings": bindings,
                }
            )
        payload = {
            "blocks": published_blocks,
            "claim_refs": tuple(sorted(published_claim_refs)),
            "field_visibility_policy_ref": self.field_visibility_policy_ref,
            "limitation_refs": tuple(sorted(limitation_refs)),
            "recommendation_refs": tuple(sorted(published_recommendation_refs)),
            "visualization_refs": self.visualization_refs,
            "warnings": self.warnings,
        }
        try:
            visibility_policy.validate_customer_payload(payload)
        except NarrativeAuthorityContractError as exc:
            raise PublicationAuthorityContractError(
                "publication_customer_payload_invalid"
            ) from exc
        return canonical_value(payload)


@dataclass(frozen=True)
class PublicationRevision:
    publication_ref: str
    revision: int
    supersedes_publication_ref: str | None
    run_attempt_id: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    narrative_id: str
    narrative_digest: str
    narrative_attempt_id: str
    local_report_ref: str
    local_report_digest: str
    projection_id: str
    projection_digest: str
    publication_digest: str
    published_at: str

    @classmethod
    def create(
        cls,
        *,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        projection: PublicationProjection,
        visibility_policy: PublicationFieldVisibilityPolicy,
        revision: int,
        supersedes_publication: PublicationRevision | None,
        published_at: str | datetime,
    ) -> "PublicationRevision":
        if type(projection) is not PublicationProjection:
            raise PublicationAuthorityContractError(
                "publication_revision_source_invalid"
            )
        projection = PublicationProjection.from_dict(
            projection.to_dict(),
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            narrative=narrative,
            local_report=local_report,
            visibility_policy=visibility_policy,
        )
        normalized_revision = _integer(
            revision,
            "publication_revision_number_invalid",
            minimum=1,
        )
        if type(supersedes_publication) is PublicationRevision:
            _assert_content_addressed_record(
                supersedes_publication,
                ref_field="publication_ref",
                digest_field="publication_digest",
                prefix="publication-revision:sha256:",
                excluded_fields=("published_at",),
                error="publication_revision_supersession_integrity_invalid",
            )
        if normalized_revision == 1:
            if supersedes_publication is not None:
                raise PublicationAuthorityContractError(
                    "publication_revision_supersession_invalid"
                )
            supersedes = None
        elif (
            type(supersedes_publication) is not PublicationRevision
            or supersedes_publication.revision != normalized_revision - 1
            or supersedes_publication.run_attempt_id != authority_bundle.run_attempt_id
            or supersedes_publication.authority_bundle_ref
            != authority_bundle.bundle_ref
            or supersedes_publication.authority_bundle_digest
            != authority_bundle.bundle_digest
        ):
            raise PublicationAuthorityContractError(
                "publication_revision_supersession_invalid"
            )
        else:
            supersedes = supersedes_publication.publication_ref
        if (
            narrative.authority_bundle_ref != authority_bundle.bundle_ref
            or projection.authority_bundle_ref != authority_bundle.bundle_ref
            or projection.authority_bundle_digest != authority_bundle.bundle_digest
            or projection.narrative_id != narrative.narrative_id
            or projection.narrative_digest != narrative.content_digest
            or projection.local_report_ref != local_report.local_report_ref
            or projection.local_report_digest != local_report.content_digest
        ):
            raise PublicationAuthorityContractError(
                "publication_revision_source_closure_invalid"
            )
        manifest = {
            "revision": normalized_revision,
            "supersedes_publication_ref": supersedes,
            "run_attempt_id": authority_bundle.run_attempt_id,
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "narrative_id": narrative.narrative_id,
            "narrative_digest": narrative.content_digest,
            "narrative_attempt_id": narrative.writer_attempt_id,
            "local_report_ref": local_report.local_report_ref,
            "local_report_digest": local_report.content_digest,
            "projection_id": projection.projection_id,
            "projection_digest": projection.projection_digest,
        }
        digest = canonical_digest(manifest)
        return cls(
            publication_ref="publication-revision:sha256:" + digest,
            publication_digest=digest,
            published_at=_aware_iso(
                published_at, "publication_revision_published_at_invalid"
            ),
            **manifest,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        projection: PublicationProjection,
        visibility_policy: PublicationFieldVisibilityPolicy,
        supersedes_publication: PublicationRevision | None,
    ) -> "PublicationRevision":
        payload = _strict_shape(payload, cls, "publication_revision_shape_invalid")
        rebuilt = cls.create(
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            narrative=narrative,
            local_report=local_report,
            projection=projection,
            visibility_policy=visibility_policy,
            revision=payload["revision"],
            supersedes_publication=supersedes_publication,
            published_at=payload["published_at"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationAuthorityContractError(
                "publication_revision_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class DeliveryOutboxRecord:
    outbox_ref: str
    run_attempt_id: str
    publication_ref: str
    publication_digest: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    projection_id: str
    projection_digest: str
    destination_ref: str
    channel: str
    idempotency_key: str
    content_digest: str

    @classmethod
    def enqueue(
        cls,
        *,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        visibility_policy: PublicationFieldVisibilityPolicy,
        supersedes_publication: PublicationRevision | None,
        publication: PublicationRevision,
        projection: PublicationProjection,
        destination_ref: str,
        channel: str,
    ) -> "DeliveryOutboxRecord":
        if type(publication) is not PublicationRevision:
            raise PublicationAuthorityContractError(
                "delivery_outbox_publication_closure_invalid"
            )
        publication = PublicationRevision.from_dict(
            publication.to_dict(),
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            narrative=narrative,
            local_report=local_report,
            projection=projection,
            visibility_policy=visibility_policy,
            supersedes_publication=supersedes_publication,
        )
        if (
            publication.projection_id != projection.projection_id
            or publication.projection_digest != projection.projection_digest
        ):
            raise PublicationAuthorityContractError(
                "delivery_outbox_publication_closure_invalid"
            )
        destination = _required_string(
            destination_ref, "delivery_outbox_destination_ref_invalid"
        )
        normalized_channel = _required_string(
            channel, "delivery_outbox_channel_invalid"
        )
        command = {
            "publication_ref": publication.publication_ref,
            "publication_digest": publication.publication_digest,
            "projection_id": projection.projection_id,
            "projection_digest": projection.projection_digest,
            "destination_ref": destination,
            "channel": normalized_channel,
        }
        idempotency_key = "delivery:sha256:" + canonical_digest(command)
        body = {
            "run_attempt_id": publication.run_attempt_id,
            **command,
            "authority_bundle_ref": publication.authority_bundle_ref,
            "authority_bundle_digest": publication.authority_bundle_digest,
            "idempotency_key": idempotency_key,
        }
        digest = canonical_digest(body)
        return cls(
            outbox_ref="delivery-outbox:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        publication: PublicationRevision,
        projection: PublicationProjection,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        visibility_policy: PublicationFieldVisibilityPolicy,
        supersedes_publication: PublicationRevision | None,
    ) -> "DeliveryOutboxRecord":
        payload = _strict_shape(payload, cls, "delivery_outbox_shape_invalid")
        rebuilt = cls.enqueue(
            publication=publication,
            projection=projection,
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            narrative=narrative,
            local_report=local_report,
            visibility_policy=visibility_policy,
            supersedes_publication=supersedes_publication,
            destination_ref=payload["destination_ref"],
            channel=payload["channel"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationAuthorityContractError("delivery_outbox_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class DeliveryAttempt:
    attempt_ref: str
    outbox_ref: str
    run_attempt_id: str
    publication_ref: str
    publication_digest: str
    projection_id: str
    projection_digest: str
    destination_ref: str
    channel: str
    idempotency_key: str
    attempt_number: int
    previous_attempt_ref: str | None
    status: str
    transport_receipt_ref: str | None
    failure_code: str | None
    attempted_at: str
    content_digest: str

    @classmethod
    def record(
        cls,
        *,
        outbox: DeliveryOutboxRecord,
        attempt_number: int,
        previous_attempt: DeliveryAttempt | None,
        status: str,
        transport_receipt_ref: str | None,
        failure_code: str | None,
        attempted_at: str | datetime,
    ) -> "DeliveryAttempt":
        if type(outbox) is not DeliveryOutboxRecord:
            raise PublicationAuthorityContractError("delivery_attempt_outbox_invalid")
        _assert_content_addressed_record(
            outbox,
            ref_field="outbox_ref",
            digest_field="content_digest",
            prefix="delivery-outbox:sha256:",
            error="delivery_attempt_outbox_integrity_invalid",
        )
        number = _integer(
            attempt_number,
            "delivery_attempt_number_invalid",
            minimum=1,
        )
        if status not in DELIVERY_ATTEMPT_STATUSES:
            raise PublicationAuthorityContractError("delivery_attempt_status_invalid")
        if number == 1:
            if previous_attempt is not None:
                raise PublicationAuthorityContractError(
                    "delivery_attempt_previous_invalid"
                )
            previous_ref = None
        else:
            if type(previous_attempt) is DeliveryAttempt:
                _assert_content_addressed_record(
                    previous_attempt,
                    ref_field="attempt_ref",
                    digest_field="content_digest",
                    prefix="delivery-attempt:sha256:",
                    error="delivery_attempt_previous_integrity_invalid",
                )
            if (
                type(previous_attempt) is not DeliveryAttempt
                or previous_attempt.outbox_ref != outbox.outbox_ref
                or previous_attempt.idempotency_key != outbox.idempotency_key
                or previous_attempt.attempt_number != number - 1
                or previous_attempt.status != "retryable_failed"
            ):
                raise PublicationAuthorityContractError(
                    "delivery_attempt_previous_invalid"
                )
            previous_ref = previous_attempt.attempt_ref
        receipt = _optional_string(
            transport_receipt_ref,
            "delivery_attempt_transport_receipt_ref_invalid",
        )
        failure = _optional_string(
            failure_code,
            "delivery_attempt_failure_code_invalid",
        )
        if (status == "published" and (receipt is None or failure is not None)) or (
            status != "published" and (receipt is not None or failure is None)
        ):
            raise PublicationAuthorityContractError("delivery_attempt_result_invalid")
        body = {
            "outbox_ref": outbox.outbox_ref,
            "run_attempt_id": outbox.run_attempt_id,
            "publication_ref": outbox.publication_ref,
            "publication_digest": outbox.publication_digest,
            "projection_id": outbox.projection_id,
            "projection_digest": outbox.projection_digest,
            "destination_ref": outbox.destination_ref,
            "channel": outbox.channel,
            "idempotency_key": outbox.idempotency_key,
            "attempt_number": number,
            "previous_attempt_ref": previous_ref,
            "status": status,
            "transport_receipt_ref": receipt,
            "failure_code": failure,
            "attempted_at": _aware_iso(
                attempted_at, "delivery_attempt_attempted_at_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            attempt_ref="delivery-attempt:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        outbox: DeliveryOutboxRecord,
        previous_attempt: DeliveryAttempt | None,
    ) -> "DeliveryAttempt":
        payload = _strict_shape(payload, cls, "delivery_attempt_shape_invalid")
        rebuilt = cls.record(
            outbox=outbox,
            attempt_number=payload["attempt_number"],
            previous_attempt=previous_attempt,
            status=payload["status"],
            transport_receipt_ref=payload["transport_receipt_ref"],
            failure_code=payload["failure_code"],
            attempted_at=payload["attempted_at"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationAuthorityContractError(
                "delivery_attempt_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def validate_publication_lifecycle(
    *,
    lifecycle: LifecycleState,
    authority_bundle: AuthorityBundle | None,
    publication: PublicationRevision | None,
    outbox: DeliveryOutboxRecord | None,
) -> None:
    if type(lifecycle) is not LifecycleState:
        raise PublicationAuthorityContractError("publication_lifecycle_state_invalid")
    try:
        lifecycle = LifecycleState.from_dict(lifecycle.to_dict())
    except ValueError as exc:
        raise PublicationAuthorityContractError(
            "publication_lifecycle_state_integrity_invalid"
        ) from exc
    if authority_bundle is not None:
        _assert_authority_bundle_integrity(
            authority_bundle,
            error="publication_lifecycle_authority_integrity_invalid",
        )
        if authority_bundle.run_attempt_id != lifecycle.run_attempt_id:
            raise PublicationAuthorityContractError(
                "publication_lifecycle_run_closure_invalid"
            )
    if publication is not None:
        _assert_content_addressed_record(
            publication,
            ref_field="publication_ref",
            digest_field="publication_digest",
            prefix="publication-revision:sha256:",
            excluded_fields=("published_at",),
            error="publication_lifecycle_revision_integrity_invalid",
        )
    if outbox is not None:
        _assert_content_addressed_record(
            outbox,
            ref_field="outbox_ref",
            digest_field="content_digest",
            prefix="delivery-outbox:sha256:",
            error="publication_lifecycle_outbox_integrity_invalid",
        )
    stable_publication = lifecycle.publication_state in {"ready", "published"}
    if stable_publication:
        if (
            authority_bundle is None
            or publication is None
            or outbox is None
            or lifecycle.execution_state != "complete"
            or lifecycle.evidence_state not in {"complete", "boundary_only"}
            or lifecycle.delivery_state == "pending"
            or publication.run_attempt_id != lifecycle.run_attempt_id
            or publication.authority_bundle_ref != authority_bundle.bundle_ref
            or publication.authority_bundle_digest != authority_bundle.bundle_digest
            or outbox.run_attempt_id != lifecycle.run_attempt_id
            or outbox.publication_ref != publication.publication_ref
            or outbox.publication_digest != publication.publication_digest
        ):
            raise PublicationAuthorityContractError(
                "publication_lifecycle_stable_closure_invalid"
            )
        if (
            lifecycle.publication_state == "published"
            and lifecycle.delivery_state != "published"
        ):
            raise PublicationAuthorityContractError(
                "publication_lifecycle_published_delivery_invalid"
            )
        return
    if publication is not None or outbox is not None:
        raise PublicationAuthorityContractError(
            "publication_lifecycle_unstable_artifact_invalid"
        )
    if lifecycle.delivery_state != "pending":
        raise PublicationAuthorityContractError(
            "publication_lifecycle_delivery_without_publication_invalid"
        )


@dataclass(frozen=True)
class NarrativeAttemptRequest:
    request_ref: str
    source_publication_ref: str
    source_publication_digest: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    source_narrative_id: str
    source_narrative_attempt_id: str
    requested_attempt_id: str
    reason_dimensions: tuple[str, ...]
    requested_by: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        publication: PublicationRevision,
        requested_attempt_id: str,
        reason_dimensions: Sequence[str],
        requested_by: str,
    ) -> "NarrativeAttemptRequest":
        if type(publication) is not PublicationRevision:
            raise PublicationAuthorityContractError(
                "narrative_attempt_request_publication_invalid"
            )
        _assert_content_addressed_record(
            publication,
            ref_field="publication_ref",
            digest_field="publication_digest",
            prefix="publication-revision:sha256:",
            excluded_fields=("published_at",),
            error="narrative_attempt_request_publication_integrity_invalid",
        )
        requested_id = _required_string(
            requested_attempt_id,
            "narrative_attempt_request_attempt_id_invalid",
        )
        if requested_id == publication.narrative_attempt_id:
            raise PublicationAuthorityContractError(
                "narrative_attempt_request_independence_invalid"
            )
        dimensions = _string_tuple(
            reason_dimensions,
            "narrative_attempt_request_dimensions_invalid",
            allow_empty=False,
        )
        if not set(dimensions).issubset(set(INSIGHT_QUALITY_DIMENSIONS)):
            raise PublicationAuthorityContractError(
                "narrative_attempt_request_dimensions_invalid"
            )
        body = {
            "source_publication_ref": publication.publication_ref,
            "source_publication_digest": publication.publication_digest,
            "authority_bundle_ref": publication.authority_bundle_ref,
            "authority_bundle_digest": publication.authority_bundle_digest,
            "source_narrative_id": publication.narrative_id,
            "source_narrative_attempt_id": publication.narrative_attempt_id,
            "requested_attempt_id": requested_id,
            "reason_dimensions": dimensions,
            "requested_by": _required_string(
                requested_by,
                "narrative_attempt_request_requested_by_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            request_ref="narrative-attempt-request:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        publication: PublicationRevision,
    ) -> "NarrativeAttemptRequest":
        payload = _strict_shape(
            payload,
            cls,
            "narrative_attempt_request_shape_invalid",
        )
        rebuilt = cls.create(
            publication=publication,
            requested_attempt_id=payload["requested_attempt_id"],
            reason_dimensions=payload["reason_dimensions"],
            requested_by=payload["requested_by"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationAuthorityContractError(
                "narrative_attempt_request_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class InsightQualityEvaluation:
    evaluation_ref: str
    source_publication_ref: str
    source_publication_digest: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    source_narrative_id: str
    source_narrative_attempt_id: str
    rubric_ref: str
    rubric_digest: str
    rubric: InsightQualityRubric
    evaluation_case_ref: str
    evaluation_case_digest: str
    evaluation_case: InsightEvaluationCaseSnapshot
    model_profile_ref: str
    model_profile_digest: str
    model_profile: InsightModelProfileSnapshot
    reviewer_ref: str
    scores: Mapping[str, int]
    human_reasons: Mapping[str, str]
    result: str
    narrative_attempt_request_ref: str | None
    advisory: bool
    reviewed_at: str
    content_digest: str

    @classmethod
    def review(
        cls,
        *,
        publication: PublicationRevision,
        rubric: InsightQualityRubric,
        evaluation_case: InsightEvaluationCaseSnapshot,
        model_profile: InsightModelProfileSnapshot,
        reviewer_ref: str,
        scores: Mapping[str, int],
        human_reasons: Mapping[str, str],
        narrative_attempt_request: NarrativeAttemptRequest | None,
        reviewed_at: str | datetime,
    ) -> "InsightQualityEvaluation":
        if type(publication) is not PublicationRevision:
            raise PublicationAuthorityContractError(
                "insight_quality_publication_invalid"
            )
        _assert_content_addressed_record(
            publication,
            ref_field="publication_ref",
            digest_field="publication_digest",
            prefix="publication-revision:sha256:",
            excluded_fields=("published_at",),
            error="insight_quality_publication_integrity_invalid",
        )
        reviewer = _required_string(
            reviewer_ref,
            "insight_quality_reviewer_ref_invalid",
        )
        normalized_scores = _scores_mapping(scores)
        try:
            normalized_rubric = InsightQualityRubric.from_dict(rubric.to_dict())
            normalized_case = InsightEvaluationCaseSnapshot.from_dict(
                evaluation_case.to_dict()
            )
            normalized_model_profile = InsightModelProfileSnapshot.from_dict(
                model_profile.to_dict()
            )
            normalized_reasons = human_reason_mapping(human_reasons)
        except (AttributeError, TypeError, InsightQualityRubricContractError) as exc:
            raise PublicationAuthorityContractError(
                "insight_quality_review_context_invalid"
            ) from exc
        if (
            type(rubric) is not InsightQualityRubric
            or type(evaluation_case) is not InsightEvaluationCaseSnapshot
            or type(model_profile) is not InsightModelProfileSnapshot
            or normalized_rubric != rubric
            or normalized_case != evaluation_case
            or normalized_model_profile != model_profile
            or normalized_case.run_attempt_id != publication.run_attempt_id
            or normalized_case.publication_ref != publication.publication_ref
            or normalized_case.publication_digest != publication.publication_digest
            or normalized_model_profile.source_publication_ref
            != publication.publication_ref
            or normalized_model_profile.source_publication_digest
            != publication.publication_digest
            or normalized_model_profile.source_narrative_id != publication.narrative_id
            or normalized_model_profile.source_narrative_attempt_id
            != publication.narrative_attempt_id
        ):
            raise PublicationAuthorityContractError(
                "insight_quality_review_context_closure_invalid"
            )
        if narrative_attempt_request is None:
            result = "retain_publication"
            request_ref = None
        else:
            try:
                narrative_attempt_request = NarrativeAttemptRequest.from_dict(
                    narrative_attempt_request.to_dict(),
                    publication=publication,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise PublicationAuthorityContractError(
                    "insight_quality_narrative_request_integrity_invalid"
                ) from exc
            if (
                type(narrative_attempt_request) is not NarrativeAttemptRequest
                or narrative_attempt_request.source_publication_ref
                != publication.publication_ref
                or narrative_attempt_request.source_publication_digest
                != publication.publication_digest
                or narrative_attempt_request.authority_bundle_ref
                != publication.authority_bundle_ref
                or narrative_attempt_request.authority_bundle_digest
                != publication.authority_bundle_digest
                or narrative_attempt_request.requested_by != reviewer
            ):
                raise PublicationAuthorityContractError(
                    "insight_quality_narrative_request_closure_invalid"
                )
            result = "request_independent_narrative_attempt"
            request_ref = narrative_attempt_request.request_ref
        body = {
            "source_publication_ref": publication.publication_ref,
            "source_publication_digest": publication.publication_digest,
            "authority_bundle_ref": publication.authority_bundle_ref,
            "authority_bundle_digest": publication.authority_bundle_digest,
            "source_narrative_id": publication.narrative_id,
            "source_narrative_attempt_id": publication.narrative_attempt_id,
            "rubric_ref": normalized_rubric.rubric_ref,
            "rubric_digest": normalized_rubric.rubric_digest,
            "rubric": normalized_rubric,
            "evaluation_case_ref": normalized_case.case_snapshot_ref,
            "evaluation_case_digest": normalized_case.case_snapshot_digest,
            "evaluation_case": normalized_case,
            "model_profile_ref": normalized_model_profile.model_profile_ref,
            "model_profile_digest": normalized_model_profile.model_profile_digest,
            "model_profile": normalized_model_profile,
            "reviewer_ref": reviewer,
            "scores": normalized_scores,
            "human_reasons": normalized_reasons,
            "result": result,
            "narrative_attempt_request_ref": request_ref,
            "advisory": True,
            "reviewed_at": _aware_iso(
                reviewed_at,
                "insight_quality_reviewed_at_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            evaluation_ref="insight-quality-evaluation:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        publication: PublicationRevision,
        narrative_attempt_request: NarrativeAttemptRequest | None,
    ) -> "InsightQualityEvaluation":
        payload = _strict_shape(
            payload,
            cls,
            "insight_quality_evaluation_shape_invalid",
        )
        try:
            rubric = InsightQualityRubric.from_dict(payload["rubric"])
            evaluation_case = InsightEvaluationCaseSnapshot.from_dict(
                payload["evaluation_case"]
            )
            model_profile = InsightModelProfileSnapshot.from_dict(
                payload["model_profile"]
            )
        except (TypeError, InsightQualityRubricContractError) as exc:
            raise PublicationAuthorityContractError(
                "insight_quality_evaluation_context_invalid"
            ) from exc
        rebuilt = cls.review(
            publication=publication,
            rubric=rubric,
            evaluation_case=evaluation_case,
            model_profile=model_profile,
            reviewer_ref=payload["reviewer_ref"],
            scores=payload["scores"],
            human_reasons=payload["human_reasons"],
            narrative_attempt_request=narrative_attempt_request,
            reviewed_at=payload["reviewed_at"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationAuthorityContractError(
                "insight_quality_evaluation_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class GuardrailPromotionRecord:
    promotion_ref: str
    governance_scope_ref: str
    evaluation_refs: tuple[str, ...]
    case_refs: tuple[str, ...]
    generalizable_pattern_ref: str
    recurrence_evidence_refs: tuple[str, ...]
    human_validation_ref: str
    business_owner_ref: str
    system_owner_ref: str
    runtime_guardrail_ref: str
    approved_at: str
    content_digest: str

    @classmethod
    def approve(
        cls,
        *,
        governance_scope_ref: str,
        evaluations: Sequence[InsightQualityEvaluation],
        generalizable_pattern_ref: str,
        recurrence_evidence_refs: Sequence[str],
        human_validation_ref: str,
        business_owner_ref: str,
        system_owner_ref: str,
        runtime_guardrail_ref: str,
        approved_at: str | datetime,
    ) -> "GuardrailPromotionRecord":
        if isinstance(evaluations, (str, bytes)) or not isinstance(
            evaluations, Sequence
        ):
            raise PublicationAuthorityContractError(
                "guardrail_promotion_evaluations_invalid"
            )
        normalized_evaluations = tuple(evaluations)
        if any(
            type(evaluation) is not InsightQualityEvaluation
            or evaluation.advisory is not True
            for evaluation in normalized_evaluations
        ):
            raise PublicationAuthorityContractError(
                "guardrail_promotion_evaluations_invalid"
            )
        for evaluation in normalized_evaluations:
            _assert_content_addressed_record(
                evaluation,
                ref_field="evaluation_ref",
                digest_field="content_digest",
                prefix="insight-quality-evaluation:sha256:",
                error="guardrail_promotion_evaluation_integrity_invalid",
            )
        evaluation_refs = tuple(
            sorted(evaluation.evaluation_ref for evaluation in normalized_evaluations)
        )
        case_refs = tuple(
            sorted(
                {
                    evaluation.evaluation_case_ref
                    for evaluation in normalized_evaluations
                }
            )
        )
        recurrence_refs = _string_tuple(
            recurrence_evidence_refs,
            "guardrail_promotion_recurrence_invalid",
            allow_empty=False,
        )
        if (
            len(evaluation_refs) < 2
            or len(evaluation_refs) != len(set(evaluation_refs))
            or len(case_refs) < 2
            or len(recurrence_refs) < 2
        ):
            raise PublicationAuthorityContractError(
                "guardrail_promotion_recurrence_invalid"
            )
        business_owner = _required_string(
            business_owner_ref,
            "guardrail_promotion_business_owner_invalid",
        )
        system_owner = _required_string(
            system_owner_ref,
            "guardrail_promotion_system_owner_invalid",
        )
        if business_owner == system_owner:
            raise PublicationAuthorityContractError(
                "guardrail_promotion_dual_ownership_invalid"
            )
        body = {
            "governance_scope_ref": _required_string(
                governance_scope_ref,
                "guardrail_promotion_governance_scope_invalid",
            ),
            "evaluation_refs": evaluation_refs,
            "case_refs": case_refs,
            "generalizable_pattern_ref": _required_string(
                generalizable_pattern_ref,
                "guardrail_promotion_pattern_ref_invalid",
            ),
            "recurrence_evidence_refs": recurrence_refs,
            "human_validation_ref": _required_string(
                human_validation_ref,
                "guardrail_promotion_human_validation_invalid",
            ),
            "business_owner_ref": business_owner,
            "system_owner_ref": system_owner,
            "runtime_guardrail_ref": _required_string(
                runtime_guardrail_ref,
                "guardrail_promotion_runtime_guardrail_ref_invalid",
            ),
            "approved_at": _aware_iso(
                approved_at,
                "guardrail_promotion_approved_at_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            promotion_ref="guardrail-promotion:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        evaluations: Sequence[InsightQualityEvaluation],
    ) -> "GuardrailPromotionRecord":
        payload = _strict_shape(
            payload,
            cls,
            "guardrail_promotion_shape_invalid",
        )
        rebuilt = cls.approve(
            governance_scope_ref=payload["governance_scope_ref"],
            evaluations=evaluations,
            generalizable_pattern_ref=payload["generalizable_pattern_ref"],
            recurrence_evidence_refs=payload["recurrence_evidence_refs"],
            human_validation_ref=payload["human_validation_ref"],
            business_owner_ref=payload["business_owner_ref"],
            system_owner_ref=payload["system_owner_ref"],
            runtime_guardrail_ref=payload["runtime_guardrail_ref"],
            approved_at=payload["approved_at"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationAuthorityContractError(
                "guardrail_promotion_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)
